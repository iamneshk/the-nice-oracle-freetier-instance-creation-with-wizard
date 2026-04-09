import configparser
import argparse
import itertools
import json
import logging
import os
import re
import shlex
import smtplib
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Union

import oci
import paramiko
from dotenv import load_dotenv
import requests

# Load environment variables from .env file
load_dotenv('oci.env')

ARM_SHAPE = "VM.Standard.A1.Flex"
E2_MICRO_SHAPE = "VM.Standard.E2.1.Micro"

# Access loaded environment variables and strip white spaces
OCI_CONFIG = os.getenv("OCI_CONFIG", "").strip()
OCT_FREE_AD = os.getenv("OCT_FREE_AD", "").strip()
DISPLAY_NAME = os.getenv("DISPLAY_NAME", "").strip()
WAIT_TIME = int(os.getenv("REQUEST_WAIT_TIME_SECS", "0").strip())
SSH_AUTHORIZED_KEYS_FILE = os.getenv("SSH_AUTHORIZED_KEYS_FILE", "").strip()
OCI_IMAGE_ID = os.getenv("OCI_IMAGE_ID", None).strip() if os.getenv("OCI_IMAGE_ID") else None
OCI_COMPUTE_SHAPE = os.getenv("OCI_COMPUTE_SHAPE", ARM_SHAPE).strip()
SECOND_MICRO_INSTANCE = os.getenv("SECOND_MICRO_INSTANCE", 'False').strip().lower() == 'true'
OCI_SUBNET_ID = os.getenv("OCI_SUBNET_ID", None).strip() if os.getenv("OCI_SUBNET_ID") else None
OPERATING_SYSTEM = os.getenv("OPERATING_SYSTEM", "").strip()
OS_VERSION = os.getenv("OS_VERSION", "").strip()
ASSIGN_PUBLIC_IP = os.getenv("ASSIGN_PUBLIC_IP", "false").strip()
BOOT_VOLUME_SIZE = os.getenv("BOOT_VOLUME_SIZE", "50").strip()
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", 'False').strip().lower() == 'true'
EMAIL = os.getenv("EMAIL", "").strip()
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "").strip()
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip()

# Read the configuration from oci_config file
config = configparser.ConfigParser()
try:
    config.read(OCI_CONFIG)
    OCI_USER_ID = config.get('DEFAULT', 'user')
    OCI_REGION = config.get('DEFAULT', 'region', fallback='').strip()
    if OCI_COMPUTE_SHAPE not in (ARM_SHAPE, E2_MICRO_SHAPE):
        raise ValueError(f"{OCI_COMPUTE_SHAPE} is not an acceptable shape")
    env_has_spaces = any(isinstance(confg_var, str) and " " in confg_var
                        for confg_var in [OCI_CONFIG, OCT_FREE_AD,WAIT_TIME,
                                SSH_AUTHORIZED_KEYS_FILE, OCI_IMAGE_ID, 
                                OCI_COMPUTE_SHAPE, SECOND_MICRO_INSTANCE, 
                                OCI_SUBNET_ID, OS_VERSION, NOTIFY_EMAIL,EMAIL,
                                EMAIL_PASSWORD, DISCORD_WEBHOOK]
                        )
    config_has_spaces = any(' ' in value for section in config.sections() 
                            for _, value in config.items(section))
    if env_has_spaces:
        raise ValueError("oci.env has spaces in values which is not acceptable")
    if config_has_spaces:
        raise ValueError("oci_config has spaces in values which is not acceptable")        

except configparser.Error as e:
    with open("ERROR_IN_CONFIG.log", "w", encoding='utf-8') as file:
        file.write(str(e))

    print(f"Error reading the configuration file: {e}")

# Set up logging
logging.basicConfig(
    filename="setup_and_info.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logging_step5 = logging.getLogger("launch_instance")
logging_step5.setLevel(logging.INFO)
fh = logging.FileHandler("launch_instance.log")
fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logging_step5.addHandler(fh)

# Set up OCI Config and Clients
oci_config_path = OCI_CONFIG if OCI_CONFIG else "~/.oci/config"
config = oci.config.from_file(oci_config_path)
iam_client = oci.identity.IdentityClient(config)
network_client = oci.core.VirtualNetworkClient(config)
compute_client = oci.core.ComputeClient(config)

IMAGE_LIST_KEYS = [
    "lifecycle_state",
    "display_name",
    "id",
    "operating_system",
    "operating_system_version",
    "size_in_mbs",
    "time_created",
]

SELECTED_IMAGE_DETAILS = {}
OCI_CALL_COUNTS = {}

ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "cyan": "\033[36m",
    "blue": "\033[34m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "magenta": "\033[35m",
}

EMOJI = {
    "progress": "🔄",
    "success": "✅",
    "warning": "⚠️",
    "error": "❌",
    "select": "📋",
}


def colorize(text, color=None, bold=False):
    """Apply ANSI color only when stdout is a terminal."""
    if not sys.stdout.isatty():
        return text
    parts = []
    if bold:
        parts.append(ANSI["bold"])
    if color and color in ANSI:
        parts.append(ANSI[color])
    parts.append(text)
    parts.append(ANSI["reset"])
    return "".join(parts)


def fail_fast_config(message):
    """Write a config error and stop immediately."""
    with open("ERROR_IN_CONFIG.log", "w", encoding="utf-8") as file:
        file.write(message)
    raise ValueError(message)


def validate_ocid(value, resource_name):
    """Validate OCI OCID-like values when they are required."""
    if not value:
        fail_fast_config(f"Missing required {resource_name}")
    if not value.startswith("ocid1."):
        fail_fast_config(f"Invalid {resource_name}: expected an OCI OCID, got '{value}'")


def validate_runtime_config():
    """Validate environment before talking to OCI."""
    if not OCI_CONFIG:
        fail_fast_config("OCI_CONFIG is required")
    if not OCT_FREE_AD:
        fail_fast_config("OCT_FREE_AD is required")
    if OCI_IMAGE_ID:
        validate_ocid(OCI_IMAGE_ID, "OCI_IMAGE_ID")
    if OCI_SUBNET_ID:
        validate_ocid(OCI_SUBNET_ID, "OCI_SUBNET_ID")


validate_runtime_config()


def log_runtime_banner():
    """Log the effective runtime configuration without secrets."""
    logging.info("=== OCI Instance Creation Start ===")
    logging.info("CONFIG_FILE: %s", OCI_CONFIG)
    logging.info("REGION: %s", OCI_REGION or "(empty)")
    logging.info("SHAPE: %s", OCI_COMPUTE_SHAPE)
    logging.info("DISPLAY_NAME: %s", DISPLAY_NAME or "(empty)")
    logging.info("FREE_AD: %s", OCT_FREE_AD or "(empty)")
    logging.info("SUBNET_MODE: %s", "provided" if OCI_SUBNET_ID else "auto-discover")
    logging.info("IMAGE_MODE: %s", "provided" if OCI_IMAGE_ID else "discover by OS/version")
    if OCI_IMAGE_ID:
        logging.info("OCI_IMAGE_ID provided: %s", OCI_IMAGE_ID)
    else:
        logging.info("IMAGE_FILTER: os=%s version=%s", OPERATING_SYSTEM or "(empty)", OS_VERSION or "(empty)")


def format_image_label(image):
    """Build a human-readable label for an OCI image."""
    os_name = f"{image.operating_system} {image.operating_system_version}"
    return f"{os_name} | {image.display_name} | {image.id}"


def format_image_date(image):
    """Format OCI image creation timestamp for display."""
    created = getattr(image, "time_created", None)
    if not created:
        return "-"
    if hasattr(created, "strftime"):
        return created.strftime("%Y-%m-%d")
    return str(created)[:10]


def print_image_table(images, title="Available OCI Images"):
    """Render a simple aligned image table."""
    print(colorize(f"\n{EMOJI['select']} {title}", "blue", bold=True))
    print(colorize(" #  OS / VERSION              DISPLAY NAME                     DATE       IMAGE ID", "magenta", bold=True))
    print(colorize("--- ------------------------ --------------------------------- ---------- --------------------------------", "dim"))
    for index, image in enumerate(images, start=1):
        os_name = f"{image.operating_system} {image.operating_system_version}"
        display_name = (image.display_name[:33] + "…") if len(image.display_name) > 34 else image.display_name
        image_id = (image.id[:36] + "…") if len(image.id) > 37 else image.id
        created = format_image_date(image)
        print(colorize(f"{index:>2}  {os_name:<24} {display_name:<33} {created:<10} {image_id}", "cyan"))
    print(colorize("=== End ===", "dim"))


def choose_image_interactively(images):
    """Prompt the user to pick an image from a numbered list."""
    print_image_table(images, title="Compatible OCI Images")

    while True:
        choice = input("Select image number (Enter = 1): ").strip()
        if not choice:
            return images[0]
        if choice.isdigit() and 1 <= int(choice) <= len(images):
            return images[int(choice) - 1]
        print(colorize(f"{EMOJI['error']} Invalid selection. Try again.", "red", bold=True))


def region_matches_image(image_region, oci_region):
    """Check whether an image region aligns with the OCI config region."""
    if not image_region or not oci_region:
        return True
    return image_region.split("-")[0] == oci_region.split("-")[0]


def filter_compatible_images(images, shape, region):
    """Filter images that are compatible with the selected shape and region."""
    compatible = []
    for image in images:
        if getattr(image, "shape", None) and image.shape != shape:
            continue
        if not region_matches_image(getattr(image, "region", ""), region):
            continue
        compatible.append(image)
    return sorted(compatible, key=lambda image: getattr(image, "time_created", None) or 0, reverse=True)


def prompt_yes_no(question, default_yes=True):
    """Ask the user a yes/no question in TTY mode."""
    suffix = "[Y/n]" if default_yes else "[y/N]"
    while True:
        answer = input(f"{question} {suffix}: ").strip().lower()
        if not answer:
            return default_yes
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer y or n.")


def prompt_non_empty(question, allow_empty=False):
    """Prompt until a value is entered unless empty is allowed."""
    while True:
        answer = input(f"{question}: ").strip()
        if answer or allow_empty:
            return answer
        print("This value is required.")


def run_wizard():
    """Collect runtime configuration interactively and save it to oci.env."""
    announce_progress("Starting interactive wizard...")
    updates = {}
    updates["OCI_CONFIG"] = prompt_non_empty(f"OCI config file path [{OCI_CONFIG or '~/.oci/config'}]", allow_empty=True) or (OCI_CONFIG or "~/.oci/config")
    updates["DISPLAY_NAME"] = prompt_non_empty(f"Instance display name [{DISPLAY_NAME or 'my-instance'}]", allow_empty=True) or (DISPLAY_NAME or "my-instance")
    updates["OCT_FREE_AD"] = prompt_non_empty(f"Availability domain suffix [{OCT_FREE_AD or 'AD-1'}]", allow_empty=True) or (OCT_FREE_AD or "AD-1")
    updates["OCI_COMPUTE_SHAPE"] = prompt_non_empty(f"Compute shape [{OCI_COMPUTE_SHAPE}]", allow_empty=True) or OCI_COMPUTE_SHAPE
    updates["SECOND_MICRO_INSTANCE"] = "True" if prompt_yes_no("Use second micro instance?", default_yes=(SECOND_MICRO_INSTANCE is True)) else "False"
    updates["REQUEST_WAIT_TIME_SECS"] = "20"
    updates["SSH_AUTHORIZED_KEYS_FILE"] = prompt_non_empty(f"SSH public key path [{SSH_AUTHORIZED_KEYS_FILE or 'auto'}]", allow_empty=True) or (SSH_AUTHORIZED_KEYS_FILE or "")
    updates["OCI_SUBNET_ID"] = prompt_non_empty("Subnet OCID (optional)", allow_empty=True)
    updates["OCI_IMAGE_ID"] = prompt_non_empty("Image OCID (optional)", allow_empty=True)
    updates["OPERATING_SYSTEM"] = prompt_non_empty(f"OS name (optional) [{OPERATING_SYSTEM}]", allow_empty=True) or OPERATING_SYSTEM
    updates["OS_VERSION"] = prompt_non_empty(f"OS version (optional) [{OS_VERSION}]", allow_empty=True) or OS_VERSION
    updates["ASSIGN_PUBLIC_IP"] = "true" if prompt_yes_no("Assign public IP?", default_yes=ASSIGN_PUBLIC_IP.lower() in ("true", "1", "y", "yes")) else "false"
    updates["BOOT_VOLUME_SIZE"] = prompt_non_empty(f"Boot volume size GB [{BOOT_VOLUME_SIZE}]", allow_empty=True) or BOOT_VOLUME_SIZE
    updates["NOTIFY_EMAIL"] = "True" if prompt_yes_no("Enable email notifications?", default_yes=NOTIFY_EMAIL) else "False"
    if updates["NOTIFY_EMAIL"] == "True":
        updates["EMAIL"] = prompt_non_empty(f"Email address [{EMAIL}]", allow_empty=True) or EMAIL
        updates["EMAIL_PASSWORD"] = prompt_non_empty("Email app password", allow_empty=False)
    updates["DISCORD_WEBHOOK"] = prompt_non_empty("Discord webhook (optional)", allow_empty=True)
    updates["TELEGRAM_TOKEN"] = prompt_non_empty("Telegram bot token (optional)", allow_empty=True)
    updates["TELEGRAM_USER_ID"] = prompt_non_empty("Telegram user id (optional)", allow_empty=True)
    update_env_file(updates)
    announce_success("Wizard values saved to oci.env")


def validate_current_config():
    """Validate the current configuration without launching OCI."""
    logging.info("Running validate-only mode")
    log_runtime_banner()
    preflight_image = type(
        "Image",
        (),
        {
            "id": OCI_IMAGE_ID or "ocid1.image.placeholder",
            "operating_system": OPERATING_SYSTEM or "(empty)",
            "operating_system_version": OS_VERSION or "(empty)",
        },
    )()
    if OCI_CONFIG and Path(OCI_CONFIG).exists():
        logging.info("OCI config file exists: yes")
    else:
        fail_fast_config("OCI config file is missing")
    if OCI_SUBNET_ID or OCI_IMAGE_ID:
        preflight_launch_checks(OCI_REGION, OCI_USER_ID, OCI_SUBNET_ID or "ocid1.subnet.placeholder", preflight_image)
    announce_success("Validation completed successfully.")


def parse_args(argv=None):
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Oracle Free Tier instance launcher")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("wizard", help="Interactively collect and save configuration")
    subparsers.add_parser("validate", help="Validate the current configuration")
    subparsers.add_parser("launch", help="Launch the instance using current configuration")

    return parser.parse_args(argv)


def announce_progress(message):
    """Show visible progress on stdout and logs."""
    print(colorize(f"{EMOJI['progress']} {message}", "cyan"), flush=True)
    logging.info(message)


def announce_success(message):
    """Show success output."""
    print(colorize(f"{EMOJI['success']} {message}", "green", bold=True), flush=True)
    logging.info(message)


def announce_warning(message):
    """Show warning output."""
    print(colorize(f"{EMOJI['warning']} {message}", "yellow", bold=True), flush=True)
    logging.info(message)


def announce_error(message):
    """Show error output."""
    print(colorize(f"{EMOJI['error']} {message}", "red", bold=True), flush=True)
    logging.error(message)


def summarize_oci_data(data):
    """Create a short human-readable OCI response summary."""
    if isinstance(data, list):
        if not data:
            return "0 items"
        first = data[0]
        bits = []
        for attr in ("id", "display_name", "lifecycle_state", "operating_system", "operating_system_version"):
            value = getattr(first, attr, None)
            if value:
                bits.append(f"{attr}={value}")
        return f"{len(data)} items; first: " + ", ".join(bits)

    bits = []
    for attr in ("id", "display_name", "lifecycle_state", "compartment_id", "availability_domain"):
        value = getattr(data, attr, None)
        if value:
            bits.append(f"{attr}={value}")
    return ", ".join(bits) if bits else str(data)[:240]


def announce_oci_error(method, data):
    """Print a readable OCI error summary."""
    message = (
        f"OCI API error during {method}: "
        f"status={data.get('status')} code={data.get('code')} message={data.get('message')}"
    )
    if data.get("opc-request-id"):
        message += f" request-id={data.get('opc-request-id')}"
    announce_error(message)
    logging.error("%s | payload=%s", message, data)


def get_call_number(method):
    """Increment and return a counter for OCI calls."""
    OCI_CALL_COUNTS[method] = OCI_CALL_COUNTS.get(method, 0) + 1
    return OCI_CALL_COUNTS[method]


def update_env_file(updates, file_path="oci.env"):
    """Update or append keys inside the OCI env file."""
    path = Path(file_path)
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    normalized_updates = {key: value for key, value in updates.items() if value is not None}
    remaining = dict(normalized_updates)
    new_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue

        key, _ = line.split("=", 1)
        key = key.strip()
        if key in remaining:
            new_lines.append(f"{key}={remaining.pop(key)}")
        else:
            new_lines.append(line)

    for key, value in remaining.items():
        new_lines.append(f"{key}={value}")

    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def persist_selected_image_to_env(image):
    """Save the selected image back into oci.env."""
    announce_progress("Saving selected image back to oci.env...")
    update_env_file(
        {
            "OCI_IMAGE_ID": image.id,
            "OPERATING_SYSTEM": image.operating_system,
            "OS_VERSION": image.operating_system_version,
        }
    )
    logging.info(
        "Saved selected image back to oci.env: %s %s (%s)",
        image.operating_system,
        image.operating_system_version,
        image.id,
    )


def save_launch_details(image, subnet_id, shape, ad_name):
    """Persist launch details for later inspection."""
    details = [
        f"Instance ID: pending",
        f"Display Name: {DISPLAY_NAME}",
        f"Availability Domain: {ad_name}",
        f"Shape: {shape}",
        f"State: launching",
        f"Image: {image.operating_system} {image.operating_system_version}",
        f"Image ID: {image.id}",
        f"Subnet ID: {subnet_id}",
        "",
    ]
    write_into_file("INSTANCE_CREATED", "\n".join(details))


def preflight_launch_checks(region, tenancy, subnet_id, image):
    """Validate the launch inputs before calling OCI."""
    logging.info("=== Preflight Check ===")
    logging.info("Region: %s", region or "(empty)")
    logging.info("Tenancy: %s", tenancy)
    logging.info("Subnet: %s", subnet_id)
    logging.info("Image ID: %s", image.id)
    logging.info("Image OS: %s %s", image.operating_system, image.operating_system_version)
    logging.info("======================")
    announce_progress("Running preflight checks before launch...")

    if not region:
        fail_fast_config("OCI region is missing from oci_config")
    if not subnet_id.startswith("ocid1.subnet."):
        fail_fast_config(f"Subnet OCID looks invalid: {subnet_id}")
    if not image.id.startswith("ocid1.image."):
        fail_fast_config(f"Image OCID looks invalid: {image.id}")
    if not tenancy.startswith("ocid1.tenancy."):
        fail_fast_config(f"Tenancy OCID looks invalid: {tenancy}")


def write_into_file(file_path, data):
    """Write data into a file.

    Args:
        file_path (str): The path of the file.
        data (str): The data to be written into the file.
    """
    with open(file_path, mode="a", encoding="utf-8") as file_writer:
        file_writer.write(data)


def send_email(subject, body, email, password):
    """Send an HTML email using the SMTP protocol.

    Args:
        subject (str): The subject of the email.
        body (str): The HTML body/content of the email.
        email (str): The sender's email address.
        password (str): The sender's email password or app-specific password.

    Raises:
        smtplib.SMTPException: If an error occurs during the SMTP communication.
    """
    # Set up the MIME
    message = MIMEMultipart()
    message["Subject"] = subject
    message["From"] = email
    message["To"] = email

    # Attach HTML content to the email
    html_body = MIMEText(body, "html")
    message.attach(html_body)

    # Connect to the SMTP server
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        try:
            # Start TLS for security
            server.starttls()
            # Login to the server
            server.login(email, password)
            # Send the email
            server.sendmail(email, email, message.as_string())
        except smtplib.SMTPException as mail_err:
            # Handle SMTP exceptions (e.g., authentication failure, connection issues)
            logging.error("Error while sending email: %s", mail_err)
            raise


def list_all_instances(compartment_id):
    """Retrieve a list of all instances in the specified compartment.

    Args:
        compartment_id (str): The compartment ID.

    Returns:
        list: The list of instances returned from the OCI service.
    """
    list_instances_response = compute_client.list_instances(compartment_id=compartment_id)
    return list_instances_response.data


def generate_html_body(instance):
    """Generate HTML body for the email with instance details.

    Args:
        instance (dict): The instance dictionary returned from the OCI service.

    Returns:
        str: HTML body for the email.
    """
    # Replace placeholders with instance details
    with open('email_content.html', 'r', encoding='utf-8') as email_temp:
        html_template = email_temp.read()
    html_body = html_template.replace('&lt;INSTANCE_ID&gt;', instance.id)
    html_body = html_body.replace('&lt;DISPLAY_NAME&gt;', instance.display_name)
    html_body = html_body.replace('&lt;AD&gt;', instance.availability_domain)
    html_body = html_body.replace('&lt;SHAPE&gt;', instance.shape)
    html_body = html_body.replace('&lt;STATE&gt;', instance.lifecycle_state)

    return html_body


def create_instance_details_file_and_notify(instance, shape=ARM_SHAPE):
    """Create a file with details of instances and notify the user.

    Args:
        instance (dict): The instance dictionary returned from the OCI service.
        shape (str): shape of the instance to be created, acceptable values are
         "VM.Standard.A1.Flex", "VM.Standard.E2.1.Micro"
    """
    details = [f"Instance ID: {instance.id}",
               f"Display Name: {instance.display_name}",
               f"Availability Domain: {instance.availability_domain}",
               f"Shape: {instance.shape}",
               f"State: {instance.lifecycle_state}",
               "\n"]
    micro_body = 'TWo Micro Instances are already existing and running'
    arm_body = '\n'.join(details)
    body = arm_body if shape == ARM_SHAPE else micro_body
    write_into_file('INSTANCE_CREATED', body)

    # Generate HTML body for email
    html_body = generate_html_body(instance)

    if NOTIFY_EMAIL:
        send_email('OCI INSTANCE CREATED', html_body, EMAIL, EMAIL_PASSWORD)


def notify_on_failure(failure_msg):
    """Notifies users when the Instance Creation Failed due to an error that's
    not handled.

    Args:
        failure_msg (msg): The error message.
    """

    mail_body = (
        "The script encountered an unhandled error and exited unexpectedly.\n\n"
        "Please launch again by executing './run.sh launch'.\n\n"
        "And raise a issue on GitHub if its not already existing:\n"
        "https://github.com/mohankumarpaluru/oracle-freetier-instance-creation/issues\n\n"
        " And include the following error message to help us investigate and resolve the problem:\n\n"
        f"{failure_msg}"
    )
    write_into_file('UNHANDLED_ERROR.log', mail_body)
    if NOTIFY_EMAIL:
        send_email('OCI INSTANCE CREATION SCRIPT: FAILED DUE TO AN ERROR', mail_body, EMAIL, EMAIL_PASSWORD)


def check_instance_state_and_write(compartment_id, shape, states=('RUNNING', 'PROVISIONING'),
                                   tries=3):
    """Check the state of instances in the specified compartment and take action when a matching instance is found.

    Args:
        compartment_id (str): The compartment ID to check for instances.
        shape (str): The shape of the instance.
        states (tuple, optional): The lifecycle states to consider. Defaults to ('RUNNING', 'PROVISIONING').
        tries(int, optional): No of reties until an instance is found. Defaults to 3.

    Returns:
        bool: True if a matching instance is found, False otherwise.
    """
    for _ in range(tries):
        instance_list = list_all_instances(compartment_id=compartment_id)
        if shape == ARM_SHAPE:
            running_arm_instance = next((instance for instance in instance_list if
                                         instance.shape == shape and instance.lifecycle_state in states), None)
            if running_arm_instance:
                create_instance_details_file_and_notify(running_arm_instance, shape)
                return True
        else:
            micro_instance_list = [instance for instance in instance_list if
                                   instance.shape == shape and instance.lifecycle_state in states]
            if len(micro_instance_list) > 1 and SECOND_MICRO_INSTANCE:
                create_instance_details_file_and_notify(micro_instance_list[-1], shape)
                return True
            if len(micro_instance_list) == 1 and not SECOND_MICRO_INSTANCE:
                create_instance_details_file_and_notify(micro_instance_list[-1], shape)
                return True       
        if tries - 1 > 0:
            time.sleep(20)

    return False


def handle_errors(command, data, log):
    """Handles errors and logs messages.

    Args:
        command (arg): The OCI command being executed.
        data (dict): The data or error information returned from the OCI service.
        log (logging.Logger): The logger instance for logging messages.

    Returns:
        bool: True if the error is temporary and the operation should be retried after a delay.
        Raises Exception for unexpected errors.
    """

    # Check for temporary errors that can be retried
    if "code" in data:
        if (data["code"] in ("TooManyRequests", "Out of host capacity.", 'InternalError')) \
                or (data["message"] in ("Out of host capacity.", "Bad Gateway")):
            log.info("Command: %s--\nOutput: %s", command, data)
            time.sleep(WAIT_TIME)
            return True

    if "status" in data and data["status"] == 502:
        log.info("Command: %s~~\nOutput: %s", command, data)
        time.sleep(WAIT_TIME)
        return True
    if data.get("code") == "NotAuthorizedOrNotFound":
        failure_msg = (
            f"OCI resource not found or not authorized during {command}.\n"
            + '\n'.join([f'{key}: {value}' for key, value in data.items()])
        )
        notify_on_failure(failure_msg)
        raise Exception(failure_msg)
    failure_msg = '\n'.join([f'{key}: {value}' for key, value in data.items()])
    notify_on_failure(failure_msg)
    # Raise an exception for unexpected errors
    raise Exception("Error: %s" % data)


def execute_oci_command(client, method, *args, **kwargs):
    """Executes an OCI command using the specified OCI client.

    Args:
        client: The OCI client instance.
        method (str): The method to call on the OCI client.
        args: Additional positional arguments to pass to the OCI client method.
        kwargs: Additional keyword arguments to pass to the OCI client method.

    Returns:
        dict: The data returned from the OCI service.

    Raises:
        Exception: Raises an exception if an unexpected error occurs.
    """
    while True:
        try:
            call_number = get_call_number(method)
            announce_progress(f"Calling OCI API [{call_number}]: {method} ...")
            response = getattr(client, method)(*args, **kwargs)
            data = response.data if hasattr(response, "data") else response
            announce_success(f"OCI API completed [{call_number}]: {method} -> {summarize_oci_data(data)}")
            return data
        except oci.exceptions.ServiceError as srv_err:
            data = {"status": srv_err.status,
                    "code": srv_err.code,
                    "message": srv_err.message,
                    "opc-request-id": getattr(srv_err, "request_id", None)}
            announce_oci_error(method, data)
            handle_errors(method, data, logging_step5)


def generate_ssh_key_pair(public_key_file: Union[str, Path], private_key_file: Union[str, Path]):
    """Generates an SSH key pair and saves them to the specified files.

    Args:
        public_key_file :file to save the public key.
        private_key_file : The file to save the private key.
    """
    key = paramiko.RSAKey.generate(2048)
    key.write_private_key_file(private_key_file)
    # Save public key to file
    write_into_file(public_key_file, (f"ssh-rsa {key.get_base64()} "
                                      f"{Path(public_key_file).stem}_auto_generated"))


def read_or_generate_ssh_public_key(public_key_file: Union[str, Path]):
    """Reads the SSH public key from the file if it exists, else generates and reads it.

    Args:
        public_key_file: The file containing the public key.

    Returns:
        Union[str, Path]: The SSH public key.
    """
    public_key_path = Path(public_key_file)

    if not public_key_path.is_file():
        logging.info("SSH key doesn't exist... Generating SSH Key Pair")
        public_key_path.parent.mkdir(parents=True, exist_ok=True)
        private_key_path = public_key_path.with_name(f"{public_key_path.stem}_private")
        generate_ssh_key_pair(public_key_path, private_key_path)

    with open(public_key_path, "r", encoding="utf-8") as pub_key_file:
        ssh_public_key = pub_key_file.read()

    return ssh_public_key


def send_discord_message(message):
    """Send a message to Discord using the webhook URL if available."""
    if DISCORD_WEBHOOK:
        payload = {"content": message}
        try:
            response = requests.post(DISCORD_WEBHOOK, json=payload)
            response.raise_for_status()
        except requests.RequestException as e:
            logging.error("Failed to send Discord message: %s", e)


def launch_instance():
    """Launches an OCI Compute instance using the specified parameters.

    Raises:
        Exception: Raises an exception if an unexpected error occurs.
    """
    # Step 1 - Get TENANCY
    logging.info("Step 1/5: resolving tenancy")
    announce_progress("Resolving OCI tenancy...")
    user_info = execute_oci_command(iam_client, "get_user", OCI_USER_ID)
    oci_tenancy = user_info.compartment_id
    logging.info("OCI_TENANCY: %s", oci_tenancy)

    # Step 2 - Get AD Name
    logging.info("Step 2/5: resolving availability domain")
    announce_progress("Resolving availability domain...")
    availability_domains = execute_oci_command(iam_client,
                                               "list_availability_domains",
                                               compartment_id=oci_tenancy)
    oci_ad_name = [item.name for item in availability_domains if
                   any(item.name.endswith(oct_ad) for oct_ad in OCT_FREE_AD.split(","))]
    if not oci_ad_name:
        fail_fast_config(f"No availability domain matched OCT_FREE_AD='{OCT_FREE_AD}'")
    oci_ad_names = itertools.cycle(oci_ad_name)
    logging.info("OCI_AD_NAME: %s", oci_ad_name)

    # Step 3 - Get Subnet ID
    logging.info("Step 3/5: resolving subnet")
    announce_progress("Resolving subnet...")
    oci_subnet_id = OCI_SUBNET_ID
    if not oci_subnet_id:
        logging.info("OCI_SUBNET_ID not provided; discovering subnet from tenancy")
        announce_warning("OCI_SUBNET_ID not provided; discovering subnet from tenancy...")
        subnets = execute_oci_command(network_client,
                                      "list_subnets",
                                      compartment_id=oci_tenancy)
        if not subnets:
            fail_fast_config("No subnet found in tenancy and OCI_SUBNET_ID is not set")
        oci_subnet_id = subnets[0].id
    logging.info("OCI_SUBNET_ID: %s", oci_subnet_id)

    # Step 4 - Get Image ID of Compute Shape
    logging.info("Step 4/5: resolving image")
    announce_progress("Resolving image selection...")
    if not OCI_IMAGE_ID:
        logging.info("OCI_IMAGE_ID not provided; listing images for shape %s", OCI_COMPUTE_SHAPE)
        announce_progress(f"Listing OCI images for shape {OCI_COMPUTE_SHAPE}...")
        images = execute_oci_command(
            compute_client,
            "list_images",
            compartment_id=oci_tenancy,
            shape=OCI_COMPUTE_SHAPE,
        )
        logging.info("Found %d candidate images for shape %s", len(images), OCI_COMPUTE_SHAPE)
        shortened_images = [{key: json.loads(str(image))[key] for key in IMAGE_LIST_KEYS
                             } for image in images]
        write_into_file('images_list.json', json.dumps(shortened_images, indent=2))
        compatible_images = filter_compatible_images(images, OCI_COMPUTE_SHAPE, OCI_REGION)
        logging.info("Compatible images after shape/region filter: %d", len(compatible_images))
        if not compatible_images:
            fail_fast_config(
                f"No images are compatible with shape {OCI_COMPUTE_SHAPE} and region {OCI_REGION}"
            )
        selected_image = None
        matching_images = [
            image for image in compatible_images
            if image.operating_system == OPERATING_SYSTEM and image.operating_system_version == OS_VERSION
        ]
        use_config_image = False
        if OPERATING_SYSTEM and OS_VERSION and matching_images:
            logging.info("Found %d image(s) matching OS='%s' version='%s'", len(matching_images), OPERATING_SYSTEM, OS_VERSION)
            announce_progress(
                f"Config specifies OS/version: {OPERATING_SYSTEM} {OS_VERSION}."
            )
            use_config_image = prompt_yes_no("Use the configured OS/version image?", default_yes=True)
            if use_config_image:
                selected_image = matching_images[0]
                logging.info("User accepted configured OS/version image")
                print_image_table([selected_image], title="Selected OCI Image")
            else:
                announce_progress("Opening full image selector...")
                selected_image = choose_image_interactively(compatible_images)
        elif OPERATING_SYSTEM and OS_VERSION and not matching_images:
            announce_warning(
                f"Configured OS/version {OPERATING_SYSTEM} {OS_VERSION} had no exact match; opening selector..."
            )
            selected_image = choose_image_interactively(compatible_images)
        elif compatible_images:
            announce_progress("No OS/version specified in config; opening selector directly...")
            selected_image = choose_image_interactively(compatible_images)
        else:
            fail_fast_config("OCI image listing returned no compatible candidates")

        print_image_table([selected_image], title="Selected OCI Image")
        oci_image_id = selected_image.id
        SELECTED_IMAGE_DETAILS.update({
            "id": selected_image.id,
            "operating_system": selected_image.operating_system,
            "operating_system_version": selected_image.operating_system_version,
            "display_name": selected_image.display_name,
        })
        persist_selected_image_to_env(selected_image)
        logging.info("OCI_IMAGE_ID: %s", oci_image_id)
    else:
        logging.info("Using user-provided OCI_IMAGE_ID")
        oci_image_id = OCI_IMAGE_ID

    logging.info("Step 5/5: preparing instance launch")
    announce_progress("Preparing launch payload...")
    assign_public_ip = ASSIGN_PUBLIC_IP.lower() in [ "true", "1", "y", "yes" ]
    logging.info("ASSIGN_PUBLIC_IP: %s", assign_public_ip)

    boot_volume_size = max(50, int(BOOT_VOLUME_SIZE))
    logging.info("BOOT_VOLUME_SIZE_GB: %s", boot_volume_size)

    ssh_public_key = read_or_generate_ssh_public_key(SSH_AUTHORIZED_KEYS_FILE)
    logging.info("SSH key loaded from: %s", SSH_AUTHORIZED_KEYS_FILE or "(auto-generated)")

    launch_image_obj = type(
        "Image",
        (),
        {
            "id": oci_image_id,
            "operating_system": SELECTED_IMAGE_DETAILS.get("operating_system", OPERATING_SYSTEM),
            "operating_system_version": SELECTED_IMAGE_DETAILS.get("operating_system_version", OS_VERSION),
        },
    )()

    preflight_launch_checks(OCI_REGION, oci_tenancy, oci_subnet_id, launch_image_obj)

    logging.info("=== Launch Payload Summary ===")
    logging.info("REGION: %s", OCI_REGION or "(empty)")
    logging.info("TENANCY: %s", oci_tenancy)
    logging.info("AD: %s", oci_ad_name[0] if oci_ad_name else "(empty)")
    logging.info("SUBNET: %s", oci_subnet_id)
    logging.info("IMAGE_ID: %s", oci_image_id)
    logging.info("IMAGE_OS: %s %s", SELECTED_IMAGE_DETAILS.get("operating_system", OPERATING_SYSTEM), SELECTED_IMAGE_DETAILS.get("operating_system_version", OS_VERSION))
    logging.info("DISPLAY_NAME: %s", DISPLAY_NAME or "(empty)")
    logging.info("SHAPE: %s", OCI_COMPUTE_SHAPE)
    logging.info("BOOT_VOLUME_SIZE_GB: %s", boot_volume_size)
    logging.info("ASSIGN_PUBLIC_IP: %s", assign_public_ip)
    logging.info("=============================")

    # Step 5 - Launch Instance if it's not already exist and running
    instance_exist_flag = check_instance_state_and_write(oci_tenancy, OCI_COMPUTE_SHAPE, tries=1)
    logging.info("Existing instance detected: %s", instance_exist_flag)

    if OCI_COMPUTE_SHAPE == "VM.Standard.A1.Flex":
        shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=4, memory_in_gbs=24)
    else:
        shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=1, memory_in_gbs=1)

    launch_attempt = 0
    while not instance_exist_flag:
        try:
            launch_attempt += 1
            logging.info("Launching instance with image=%s subnet=%s", oci_image_id, oci_subnet_id)
            announce_progress(f"Calling Oracle Compute launch_instance API (attempt {launch_attempt})...")
            launch_instance_response = compute_client.launch_instance(
                launch_instance_details=oci.core.models.LaunchInstanceDetails(
                    availability_domain=next(oci_ad_names),
                    compartment_id=oci_tenancy,
                    create_vnic_details=oci.core.models.CreateVnicDetails(
                        assign_public_ip=assign_public_ip,
                        assign_private_dns_record=True,
                        display_name=DISPLAY_NAME,
                        subnet_id=oci_subnet_id,
                    ),
                    display_name=DISPLAY_NAME,
                    shape=OCI_COMPUTE_SHAPE,
                    availability_config=oci.core.models.LaunchInstanceAvailabilityConfigDetails(
                        recovery_action="RESTORE_INSTANCE"
                    ),
                    instance_options=oci.core.models.InstanceOptions(
                        are_legacy_imds_endpoints_disabled=False
                    ),
                    shape_config=shape_config,
                    source_details=oci.core.models.InstanceSourceViaImageDetails(
                        source_type="image",
                        image_id=oci_image_id,
                        boot_volume_size_in_gbs=boot_volume_size,
                    ),
                    metadata={
                        "ssh_authorized_keys": ssh_public_key},
                )
            )
            if launch_instance_response.status == 200:
                logging_step5.info(
                    "Command: launch_instance\nOutput: %s", launch_instance_response
                )
                announce_success(
                    f"Launch request accepted by OCI. Status={launch_instance_response.status}, "
                    f"request-id={launch_instance_response.headers.get('opc-request-id') if hasattr(launch_instance_response, 'headers') else 'n/a'}"
                )
                announce_progress("Polling for instance state...")
                if SELECTED_IMAGE_DETAILS:
                    save_launch_details(
                        image=type("Image", (), SELECTED_IMAGE_DETAILS)(),
                        subnet_id=oci_subnet_id,
                        shape=OCI_COMPUTE_SHAPE,
                        ad_name=next(iter(oci_ad_name), "") if oci_ad_name else "",
                    )
                instance_exist_flag = check_instance_state_and_write(oci_tenancy, OCI_COMPUTE_SHAPE)
                announce_success(f"Instance state check completed. Exists={instance_exist_flag}")

        except oci.exceptions.ServiceError as srv_err:
            if srv_err.code == "LimitExceeded":                
                logging_step5.info("Encoundered LimitExceeded Error checking if instance is created" \
                                   "code :%s, message: %s, status: %s", srv_err.code, srv_err.message, srv_err.status)                
                announce_progress("OCI returned LimitExceeded; checking whether the instance already exists...")
                instance_exist_flag = check_instance_state_and_write(oci_tenancy, OCI_COMPUTE_SHAPE)
                if instance_exist_flag:
                    logging_step5.info("%s , exiting the program", srv_err.code)
                    announce_success("Instance already exists; exiting cleanly.")
                    sys.exit()
                logging_step5.info("Didn't find an instance , proceeding with retries")     
            announce_oci_error("launch_instance", {
                "status": srv_err.status,
                "code": srv_err.code,
                "message": srv_err.message,
                "opc-request-id": getattr(srv_err, "request_id", None),
            })
            data = {
                "status": srv_err.status,
                "code": srv_err.code,
                "message": srv_err.message,
                "opc-request-id": getattr(srv_err, "request_id", None),
            }
            handle_errors("launch_instance", data, logging_step5)


if __name__ == "__main__":
    try:
        args = parse_args()
        if args.command == "wizard":
            run_wizard()
        elif args.command == "validate":
            validate_current_config()
        else:
            send_discord_message("🚀 OCI Instance Creation Script: Starting up! Let's create some cloud magic!")
            launch_instance()
            send_discord_message("🎉 Success! OCI Instance has been created. Time to celebrate!")
    except Exception as e:
        error_message = f"😱 Oops! Something went wrong with the OCI Instance Creation Script:\n{str(e)}"
        send_discord_message(error_message)
        raise
