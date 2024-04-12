import os
import sys
import subprocess
import pathlib
import re
import traceback

import docker
import yaml
from kubernetes import client
from flask import request
from flask_restx import Namespace, Resource
from CTFd.utils.user import get_current_user, is_admin
from CTFd.utils.decorators import authed_only

from ...config import HOST_DATA_PATH, INTERNET_FOR_ALL, WINDOWS_VM_ENABLED, SECCOMP, USER_FIREWALL_ALLOWED
from ...models import Dojos, DojoModules, DojoChallenges
from ...utils import serialize_user_flag, simple_tar, random_home_path, module_challenges_visible, user_ipv4
from ...utils.dojo import dojo_accessible, get_current_dojo_challenge


docker_namespace = Namespace(
    "docker", description="Endpoint to manage docker containers"
)


def start_challenge(user, dojo_challenge, practice):
    # TODO: Remove this docker-based implementation in favor of kube

    def exec_run(cmd, *, shell=False, assert_success=True, user="root", **kwargs):
        if shell:
            cmd = f"""/bin/sh -c \"
            {cmd}
            \""""
        exit_code, output = container.exec_run(cmd, user=user, **kwargs)
        if assert_success:
            assert exit_code in (0, None), output
        return exit_code, output

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

        devices = []
        if os.path.exists("/dev/kvm"):
            devices.append("/dev/kvm:/dev/kvm:rwm")
        if os.path.exists("/dev/net/tun"):
            devices.append("/dev/net/tun:/dev/net/tun:rwm")

        container = docker_client.containers.create(
            dojo_challenge.image,
            entrypoint=["/bin/sleep", "6h"],
            name=f"user_{user.id}",
            hostname=hostname,
            user="hacker",
            working_dir="/home/hacker",
            labels={
                "dojo.dojo_id": dojo_challenge.dojo.reference_id,
                "dojo.module_id": dojo_challenge.module.id,
                "dojo.challenge_id": dojo_challenge.id,
                "dojo.challenge_description": dojo_challenge.description,
                "dojo.user_id": str(user.id),
                "dojo.mode": "privileged" if practice else "standard",
                "dojo.auth_token": os.urandom(32).hex(),
            },
            mounts=[
                docker.types.Mount(
                    "/home/hacker",
                    f"{HOST_DATA_PATH}/homes/nosuid/{random_home_path(user)}",
                    "bind",
                    propagation="shared",
                )
            ]
            + (
                [
                    docker.types.Mount(
                        target="/run/media/windows",
                        source="pwncollege_windows",
                        read_only=True,
                    )
                ]
                if WINDOWS_VM_ENABLED
                else []
            ),
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
            mem_limit="4000m",
            detach=True,
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
        exit_code, output = exec_run("findmnt --output OPTIONS /home/hacker",
                                     assert_success=False)
        if exit_code != 0:
            container.kill()
            container.wait(condition="removed")
            raise RuntimeError("Home directory failed to mount")
        if b"nosuid" not in output:
            container.kill()
            container.wait(condition="removed")
            raise RuntimeError("Home directory failed to mount as nosuid")

    def grant_sudo():
        exec_run(
            """
            chmod 4755 /usr/bin/sudo
            usermod -aG sudo hacker
            echo 'hacker ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers
            passwd -d root
            """,
            shell=True
        )

    def insert_challenge(user, dojo_challenge):
        for path in dojo_challenge.challenge_paths(user):
            with simple_tar(path, f"/challenge/{path.name}") as tar:
                container.put_archive("/", tar)
        exec_run("chown -R root:root /challenge")
        exec_run("chmod -R 4755 /challenge")

    def insert_flag(flag):
        exec_run(f"echo 'pwn.college{{{flag}}}' > /flag", shell=True)

    def insert_auth_token(auth_token):
        exec_run(f"echo '{auth_token}' > /.authtoken", shell=True)

    def initialize_container():
        exec_run(
            """
            /opt/pwn.college/docker-initialize.sh

            if [ -x "/challenge/.init" ]; then
                /challenge/.init
            fi

            touch /opt/pwn.college/.initialized
            """,
            shell=True
        )
        exec_run(
            """
            /opt/pwn.college/docker-entrypoint.sh &
            """,
            shell=True,
            user="hacker"
        )

    setup_home(user)

    container = start_container(user, dojo_challenge, practice)

    verify_nosuid_home()

    if practice:
        grant_sudo()

    insert_challenge(user, dojo_challenge)

    flag = "practice" if practice else serialize_user_flag(user.id, dojo_challenge.challenge_id)
    insert_flag(flag)

    auth_token = container.metadata.annotations["dojo/auth_token"]
    insert_auth_token(auth_token)

    initialize_container()


def start_challenge(user, dojo_challenge, practice):
    namespace = "default"

    def setup_home():
        # TODO-KUBE: we don't actually care about nosuid or random_home_path anymore
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

    def start_container():
        # TODO-KUBE: Latest forces a pull, we should make the pull async
        image = "registry:5000/challenge:latest"
        name = f"user-{user.id}"
        # TODO-KUBE: Upstream changed the hostname
        hostname = "-".join((dojo_challenge.module.id, dojo_challenge.id))
        if practice:
            hostname = f"practice~{hostname}"

        flag = "practice" if practice else serialize_user_flag(user.id, dojo_challenge.challenge_id)
        config = dict(
            flag=f"pwn.college{{{flag}}}",
            privileged=str(bool(practice)).lower(),
            auth_token=os.urandom(32).hex(),
        )

        container = client.V1Container(
            name=name,
            image=image,
            env=[
                client.V1EnvVar(name=f"DOJO_{name.upper()}", value=value)
                for name, value in config.items()
            ],
            volume_mounts=[
                client.V1VolumeMount(name="home", mount_path=f"/home/hacker"),
                client.V1VolumeMount(name="kvm", mount_path="/dev/kvm"),
            ],
            resources=client.V1ResourceRequirements(
                limits=dict(cpu="4000m", memory="4000Mi"),
                requests=dict(cpu="100m", memory="100Mi"),
            ),
            # TODO-KUBE: seccomp
            security_context=client.V1SecurityContext(
                capabilities=client.V1Capabilities(add=["SYS_PTRACE"]),
            ),
        )

        home_volume = client.V1Volume(
            name="home",
            nfs=client.V1NFSVolumeSource(
                server="homes-nfs",
                path=f"/nosuid/{random_home_path(user)}",
            )
        )

        kvm_volume = client.V1Volume(
            name="kvm",
            host_path=client.V1HostPathVolumeSource(path="/dev/kvm", type="CharDevice"),
        )

        annotations = dict(
            dojo_id=dojo_challenge.dojo.reference_id,
            module_id=dojo_challenge.module.id,
            challenge_id=dojo_challenge.id,
            challenge_description=dojo_challenge.description,
            user_id=str(user.id),
            **config,
        )
        annotations = {f"dojo/{k}": v for k, v in annotations.items()}

        pod = client.V1Pod(
            api_version="v1",
            kind="Pod",
            metadata=client.V1ObjectMeta(name=name, annotations=annotations),
            spec=client.V1PodSpec(
                hostname=hostname,
                containers=[container],
                volumes=[home_volume, kvm_volume],
                restart_policy="Never",
                automount_service_account_token=False,
                enable_service_links=False,
            ),
        )

        api_instance = client.CoreV1Api()

        try:
            api_instance.delete_namespaced_pod(
                name=name,
                namespace=namespace,
                body=client.V1DeleteOptions(grace_period_seconds=0),
            )
        except client.exceptions.ApiException:
            pass

        # TODO-KUBE: This is async, we might be in "ContainerCreating" state for a while before "Running"
        api_instance.create_namespaced_pod(namespace=namespace, body=pod)

    setup_home()
    start_container()


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
