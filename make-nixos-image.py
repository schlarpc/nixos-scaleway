#!/usr/bin/env python3

import argparse
import datetime
import logging
import os
import pathlib
import queue
import re
import threading
import time

import paramiko
import scaleway.apis
import tenacity


logger = logging.getLogger(__name__)


def get_minimal_ubuntu(all_images, region, instance_type):
    # find likely "base Ubuntu" images
    images = [
        image
        for image in all_images
        if "ubuntu" in image["name"].lower() and "distribution" in image["categories"]
    ]
    # newly created entries first, to find the latest "major release"
    images.sort(key=lambda image: image["creation_date"], reverse=True)
    # get "public" version for each image
    public_versions = [
        version
        for image in images
        for version in image["versions"]
        if version["id"] == image["current_public_version"]
    ]
    # find disk images compatible with selected instance type and region
    compatible_local_images = [
        image
        for version in public_versions
        for image in version["local_images"]
        if all(
            (
                (instance_type in image["compatible_commercial_types"]),
                # HACK: backcompat region format (e.g. par1 from fr-par-1)
                (image["zone"] in [region, "".join(region.split("-")[-2:])]),
            )
        )
    ]
    if compatible_local_images:
        return compatible_local_images[0]["id"]
    raise Exception("Image not found")


@tenacity.retry(stop=tenacity.stop_after_attempt(30))
def ssh_connect(ip_address, private_key):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy)
    client.connect(hostname=ip_address, username="root", pkey=private_key, timeout=5)
    return client


def read_lines(streams):
    q = queue.Queue()

    def read_stream(stream):
        for line in stream:
            q.put(line)

    threads = [
        threading.Thread(target=read_stream, args=(stream,), daemon=True)
        for stream in streams
    ]
    for thread in threads:
        thread.start()

    while True:
        try:
            yield q.get(timeout=0.01)
        except queue.Empty:
            if not any(thread.is_alive() for thread in threads):
                break


def flatten_whitespace(lines):
    yield from filter(None, (re.sub("\s+", " ", l).strip() for l in lines))


def get_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--secret-key",
        required=("SCW_SECRET_KEY" not in os.environ),
        **(
            {"default": os.environ["SCW_SECRET_KEY"]}
            if "SCW_SECRET_KEY" in os.environ
            else {}
        ),
    )
    parser.add_argument("--region", default="fr-par-1")
    parser.add_argument("--instance-type", default="DEV1-M")
    parser.add_argument("--bootstrap-disk-size", default=20)
    return parser.parse_args(argv)


def main(argv=None):
    args = get_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d :: [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    account = scaleway.apis.AccountAPI(auth_token=args.secret_key)
    marketplace = scaleway.apis.API(base_url="https://api-marketplace.scaleway.com/")
    compute = scaleway.apis.ComputeAPI(
        auth_token=args.secret_key, base_url="https://api.scaleway.com/"
    )

    organization_id = account.query().organizations.get()["organizations"][0]["id"]
    logger.info("Using organization ID %s", organization_id)

    image_id = get_minimal_ubuntu(
        marketplace.query().images.get()["images"], args.region, args.instance_type
    )
    logger.info("Using bootstrap (Ubuntu) image ID %s", image_id)

    private_key = paramiko.ECDSAKey.generate(bits=256)
    server = (
        compute.query()
        .instance.v1.zones(args.region)
        .servers.post(
            {
                "organization": organization_id,
                "name": "nixos-image-builder",
                "image": image_id,
                "commercial_type": args.instance_type,
                "volumes": {
                    "0": {"size": 1_000_000_000 * args.bootstrap_disk_size},
                    "1": {
                        "name": "nixos-volume",
                        "organization": organization_id,
                        "volume_type": "l_ssd",
                        "size": 20_000_000_000,
                    },
                },
                "boot_type": "local",
                "tags": [
                    "AUTHORIZED_KEY="
                    + private_key.get_name()
                    + "_"
                    + private_key.get_base64()
                ],
            }
        )["server"]
    )
    logger.info("Provisioned instance %s", server["id"])

    logger.info("Starting instance, this may take a bit...")
    response = (
        compute.query()
        .instance.v1.zones(args.region)
        .servers(server["id"])
        .action.post({"action": "poweron"})
    )

    while True:
        server = (
            compute.query()
            .instance.v1.zones(args.region)
            .servers(server["id"])
            .get()["server"]
        )
        if server["state"] == "running":
            break
        time.sleep(1)
    logger.info("Instance running")

    logger.info("Attempting to SSH to root@%s", server["public_ip"]["address"])
    client = ssh_connect(server["public_ip"]["address"], private_key)

    logger.info("Copying bootstrap files")
    sftp = client.open_sftp()
    bootstrap_file_path = pathlib.Path(__file__).parent / "bootstrap"
    for bootstrap_file in bootstrap_file_path.glob("*"):
        filename = bootstrap_file.relative_to(bootstrap_file_path)
        source = str(bootstrap_file.resolve())
        destination = str(pathlib.PurePosixPath("/tmp") / filename)
        sftp.put(source, destination)

    logger.info("Executing NixOS bootstrap")
    _, stdout, stderr = client.exec_command("bash /tmp/nix-bootstrap.sh")
    for line in flatten_whitespace(read_lines((stdout, stderr))):
        logger.info(line)
    status = stdout.channel.recv_exit_status()
    logger.info("Bootstrap exited with status %d", status)

    if status not in (-1, 0):
        raise Exception("Failed to bootstrap")

    logger.info("Waiting for instance to stop...")
    while True:
        server = (
            compute.query()
            .instance.v1.zones(args.region)
            .servers(server["id"])
            .get()["server"]
        )
        if server["state"] == "stopped in place":
            break
        time.sleep(1)

    image_name = (
        "nixos-" + datetime.datetime.utcnow().replace(microsecond=0).isoformat()
    )
    snapshot = (
        compute.query()
        .instance.v1.zones(args.region)
        .snapshots.post(
            {
                "volume_id": server["volumes"]["1"]["id"],
                "organization": organization_id,
                "name": image_name,
            }
        )["snapshot"]
    )
    logger.info("Created snapshot ID %s", snapshot["id"])

    logger.info("Waiting for snapshot to become available")
    while True:
        snapshot = (
            compute.query()
            .instance.v1.zones(args.region)
            .snapshots(snapshot["id"])
            .get()["snapshot"]
        )
        if snapshot["state"] == "available":
            break
        time.sleep(1)

    image = (
        compute.query()
        .instance.v1.zones(args.region)
        .images.post(
            {
                "name": image_name,
                "root_volume": snapshot["id"],
                "arch": server["arch"],
                "organization": organization_id,
            }
        )["image"]
    )
    logger.info("Created NixOS image ID %s", image["id"])

    logger.info("Deleting server ID %s", server["id"])
    compute.query().instance.v1.zones(args.region).servers(server["id"]).action.post(
        {"action": "terminate"}
    )

    logger.info("Done")


if __name__ == "__main__":
    main()
