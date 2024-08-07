import hashlib
import os
import pathlib
import re
import subprocess
import sys
import traceback

import docker
from flask import request, current_app
from flask_restx import Namespace, Resource
from CTFd.utils.user import get_current_user, is_admin
from CTFd.utils.decorators import authed_only

from ...config import HOST_DATA_PATH, INTERNET_FOR_ALL, WINDOWS_VM_ENABLED, SECCOMP, USER_FIREWALL_ALLOWED
from ...models import Dojos, DojoModules, DojoChallenges
from ...utils import serialize_user_flag, resolved_tar, random_home_path, module_challenges_visible, user_ipv4
from ...utils.dojo import dojo_accessible, get_current_dojo_challenge
from ...utils.workspace import exec_run

docker_namespace = Namespace(
    "docker", description="Endpoint to manage docker containers"
)


def start_challenge(user, dojo_challenge, practice):
    def setup_home(user):
        homes = pathlib.Path("/var/homes")
        homefs = homes / "homefs"
        user_data = homes / "data" / str(user.id)
        user_nosuid = homes / "nosuid" / random_home_path(user)

        assert homefs.exists()
        user_data.parent.mkdir(exist_ok=True)
        user_nosuid.parent.mkdir(exist_ok=True)

        if not user_data.exists():
            # Shell out to `cp` in order to sparsely copy
            subprocess.run(["cp", homefs, user_data], check=True)

        process = subprocess.run(
            ["findmnt", "--output", "OPTIONS", user_nosuid], capture_output=True
        )
        if b"nosuid" not in process.stdout:
            subprocess.run(
                ["mount", user_data, "-o", "nosuid,X-mount.mkdir", user_nosuid],
                check=True,
            )

    def start_container(user, dojo_challenge, practice):
        docker_client = docker.from_env()
        try:
            container_name = f"user_{user.id}"
            container = docker_client.containers.get(container_name)
            container.remove(force=True)
            container.wait(condition="removed")
        except docker.errors.NotFound:
            pass

        hostname = "~".join((["practice"] if practice else []) + [
            dojo_challenge.module.id,
            re.sub("[\s.-]+", "-", re.sub("[^a-z0-9\s.-]", "", dojo_challenge.name.lower()))
        ])[:64]

        auth_token = os.urandom(32).hex()

        challenge_bin_path = "/run/challenge/bin"
        system_bin_path = "/run/current-system/sw/bin"
        image = docker_client.images.get(dojo_challenge.image)
        environment = image.attrs["Config"].get("Env") or []
        for env_var in environment:
            if env_var.startswith("PATH="):
                env_paths = env_var[len("PATH="):].split(":")
                env_paths = [challenge_bin_path, system_bin_path, *env_paths]
                break
        else:
            env_paths = [system_bin_path]
        env_path = ":".join(env_paths)

        devices = []
        if os.path.exists("/dev/kvm"):
            devices.append("/dev/kvm:/dev/kvm:rwm")
        if os.path.exists("/dev/net/tun"):
            devices.append("/dev/net/tun:/dev/net/tun:rwm")

        storage_driver = docker_client.info().get("Driver")

        container = docker_client.containers.create(
            dojo_challenge.image,
            entrypoint=["/nix/var/nix/profiles/default/bin/dojo-init", f"{system_bin_path}/sleep", "6h"],
            name=f"user_{user.id}",
            hostname=hostname,
            user="0",
            working_dir="/home/hacker",
            environment={
                "HOME": "/home/hacker",
                "PATH": env_path,
                "SHELL": f"{system_bin_path}/bash",
                "DOJO_AUTH_TOKEN": auth_token,
                "DOJO_MODE": "privileged" if practice else "standard",
            },
            labels={
                "dojo.dojo_id": dojo_challenge.dojo.reference_id,
                "dojo.module_id": dojo_challenge.module.id,
                "dojo.challenge_id": dojo_challenge.id,
                "dojo.challenge_description": dojo_challenge.description,
                "dojo.user_id": str(user.id),
                "dojo.auth_token": auth_token,
                "dojo.mode": "privileged" if practice else "standard",
            },
            mounts=[
                docker.types.Mount(
                    "/nix",
                    f"{HOST_DATA_PATH}/workspace/nix",
                    "bind",
                    read_only=True,
                ),
                docker.types.Mount(
                    "/home/hacker",
                    f"{HOST_DATA_PATH}/homes/nosuid/{random_home_path(user)}",
                    "bind",
                    propagation="shared",
                ),
            ],
            devices=devices,
            network=None,
            extra_hosts={
                hostname: "127.0.0.1",
                "vm": "127.0.0.1",
                f"vm_{hostname}"[:64]: "127.0.0.1",
                "challenge.localhost": "127.0.0.1",
                "hacker.localhost": "127.0.0.1",
                "dojo-user": user_ipv4(user),
                **USER_FIREWALL_ALLOWED,
            },
            init=True,
            cap_add=["SYS_PTRACE"],
            security_opt=[f"seccomp={SECCOMP}"],
            cpu_period=100000,
            cpu_quota=400000,
            pids_limit=1024,
            mem_limit="4G",
            detach=True,
            stdin_open=True,
            auto_remove=True,
        )

        user_network = docker_client.networks.get("user_network")
        user_network.connect(container, ipv4_address=user_ipv4(user), aliases=[f"user_{user.id}"])

        default_network = docker_client.networks.get("bridge")
        internet_access = INTERNET_FOR_ALL or any(award.name == "INTERNET" for award in user.awards)
        if not internet_access:
            default_network.disconnect(container)

        container.start()
        return container

    def verify_nosuid_home():
        exit_code, output = exec_run(
            "/run/current-system/sw/bin/findmnt --output OPTIONS /home/hacker",
            container=container,
            assert_success=False
        )
        if exit_code != 0:
            container.kill()
            container.wait(condition="removed")
            raise RuntimeError("Home directory failed to mount")
        if b"nosuid" not in output:
            container.kill()
            container.wait(condition="removed")
            raise RuntimeError("Home directory failed to mount as nosuid")

    def insert_challenge(user, dojo_challenge):
        def is_option_path(path):
            path = pathlib.Path(*path.parts[:len(dojo_challenge.path.parts) + 1])
            return path.name.startswith("_") and path.is_dir()

        exec_run("/run/current-system/sw/bin/mkdir -p /challenge", container=container)

        root_dir = dojo_challenge.path.parent.parent
        challenge_tar = resolved_tar(dojo_challenge.path,
                                     root_dir=root_dir,
                                     filter=lambda path: not is_option_path(path))
        container.put_archive("/challenge", challenge_tar)

        option_paths = sorted(path for path in dojo_challenge.path.iterdir() if is_option_path(path))
        if option_paths:
            secret = current_app.config["SECRET_KEY"]
            option_hash = hashlib.sha256(f"{secret}_{user.id}_{dojo_challenge.challenge_id}".encode()).digest()
            option = option_paths[int.from_bytes(option_hash[:8], "little") % len(option_paths)]
            container.put_archive("/challenge", resolved_tar(option, root_dir=root_dir))

        exec_run("/run/current-system/sw/bin/chown -R 0:0 /challenge", container=container)
        exec_run("/run/current-system/sw/bin/chmod -R 4755 /challenge", container=container)

    def insert_flag(flag):
        flag = f"pwn.college{{{flag}}}"
        socket = container.attach_socket(params=dict(stdin=1, stream=1))
        socket._sock.sendall(flag.encode() + b"\n")
        socket.close()

    setup_home(user)

    container = start_container(user, dojo_challenge, practice)

    verify_nosuid_home()

    insert_challenge(user, dojo_challenge)

    flag = "practice" if practice else serialize_user_flag(user.id, dojo_challenge.challenge_id)
    insert_flag(flag)


@docker_namespace.route("")
class RunDocker(Resource):
    @authed_only
    def post(self):
        data = request.get_json()
        dojo_id = data.get("dojo")
        module_id = data.get("module")
        challenge_id = data.get("challenge")
        practice = data.get("practice")

        user = get_current_user()

        dojo = dojo_accessible(dojo_id)
        if not dojo:
            return {"success": False, "error": "Invalid dojo"}

        dojo_challenge = (
            DojoChallenges.query.filter_by(id=challenge_id)
            .join(DojoModules.query.filter_by(dojo=dojo, id=module_id).subquery())
            .first()
        )
        if not dojo_challenge:
            return {"success": False, "error": "Invalid challenge"}

        if not dojo_challenge.visible() and not dojo.is_admin():
            return {"success": False, "error": "Invalid challenge"}

        if practice and not dojo_challenge.allow_privileged:
            return {"success": False, "error": "This challenge does not support practice mode."}

        try:
            start_challenge(user, dojo_challenge, practice)
        except RuntimeError as e:
            print(f"ERROR: Docker failed for {user.id}: {e}", file=sys.stderr, flush=True)
            traceback.print_exc(file=sys.stderr)
            return {"success": False, "error": str(e)}
        except Exception as e:
            print(f"ERROR: Docker failed for {user.id}: {e}", file=sys.stderr, flush=True)
            traceback.print_exc(file=sys.stderr)
            return {"success": False, "error": "Docker failed"}
        return {"success": True}

    @authed_only
    def get(self):
        dojo_challenge = get_current_dojo_challenge()
        if not dojo_challenge:
            return {"success": False, "error": "No active challenge"}
        return {
            "success": True,
            "dojo": dojo_challenge.dojo.reference_id,
            "module": dojo_challenge.module.id,
            "challenge": dojo_challenge.id
        }
