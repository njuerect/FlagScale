import os
import re
import sys
import collections
import copy
import socket
import shlex
import subprocess
import json
import uuid
from datetime import datetime
from abc import ABC, abstractmethod
from omegaconf import DictConfig, OmegaConf
from ..logger import logger


def log_and_raise_error(message):
    logger.error(message)
    raise ValueError(message)


def parse_hostfile(hostfile_path):
    if hostfile_path is None or not os.path.isfile(hostfile_path):
        logger.warning(
            "Hostfile not found. The training will proceed using only local resources."
        )
        return None

    # e.g., worker0 slots=8 type=A100
    pattern = re.compile(r"^(\S+)\s+slots=(\d+)(?:\s+type=(\S+))?")

    resources = collections.OrderedDict()

    with open(hostfile_path, "r") as fd:
        hostfile_lines = fd.readlines()

    for line in hostfile_lines:
        line = line.strip()
        match = pattern.search(line)
        if line.startswith("#") or line == "":
            # hostfile comment or empty line, ignore
            continue
        elif match:
            host = match.group(1)
            num_slots = int(match.group(2))
            machine_type = match.group(3) if match.group(3) else None
            if host in resources:
                log_and_raise_error(
                    f"Hostfile contains multiple entries for host: {host}."
                )
            resources[host] = {"slots": num_slots, "type": machine_type}
        else:
            log_and_raise_error(f"Invalid entry in hostfile: {line}.")

    if len(resources) == 0:
        log_and_raise_error(
            "Hostfile is empty or not formatted correctly. Please check the hostfile."
        )

    return resources


def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def get_host_name_or_ip():
    host_name = socket.gethostname()
    if host_name:
        return host_name
    try:
        # doesn't even have to be reachable
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("10.255.255.255", 1))
        IP = sock.getsockname()[0]
    except Exception:
        IP = "127.0.0.1"
    finally:
        if 'sock' in locals():  # Ensure 'sock' was successfully created before attempting to close it
            sock.close()
    return IP


def run_local_command(cmd, dryrun=False):
    logger.info(f"Run the local command: {cmd}")
    if dryrun:
        return
    result = subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Command {cmd} failed with return code {result.returncode}.")
        print(f"Output: {result.stdout}")
        print(f"Error: {result.stderr}")
        sys.exit(result.returncode)


def run_ssh_command(host, cmd, port=None, dryrun=False):
    if port:
        ssh_cmd = f"ssh -f -n -p {port} {host} '{cmd}'"
    else:
        ssh_cmd = f"ssh -f -n {host} '{cmd}'"
    logger.info(f"Run the ssh command: {ssh_cmd}")
    if dryrun:
        return
    subprocess.run(ssh_cmd, shell=True, check=True)


def run_scp_command(host, src, dst, port=None, dryrun=False):
    if port:
        scp_cmd = f"scp -P {port} -r {src} {host}:{dst} "
    else:
        scp_cmd = f"scp -r {src} {host}:{dst} "
    logger.info(f"Run the scp command: {scp_cmd}")
    if dryrun:
        return
    subprocess.run(scp_cmd, shell=True, check=True)


def _flatten_dict_to_args(config_dict, ignore_keys=[]):
    args = []
    for key, value in config_dict.items():
        if key in ignore_keys:
            continue
        key = key.replace("_", "-")
        if isinstance(value, dict):
            args.extend(_flatten_dict_to_args(value, ignore_keys))
        elif isinstance(value, list):
            args.append(f"--{key}")
            for v in value:
                args.append(f"{v}")
        elif isinstance(value, bool):
            if value:
                args.append(f"--{key}")
        else:
            args.append(f"--{key}")
            args.append(f"{value}")
    return args


def get_megatron_args(config: DictConfig):
    assert (
        config.experiment.task.backend == "megatron"
    ), "This function only supports megatron backend."

    # Convert the DictConfig to a regular dictionary
    config_dict = OmegaConf.to_container(config, resolve=True)
    config_dict = config_dict["train"]

    new_config_dict = {}
    new_config_dict.update(config_dict["system"])
    new_config_dict.update(config_dict["model"])
    new_config_dict.update(config_dict["data"])

    ignore_keys = ["log_dir", "details_dir", "scripts_dir", "pids_dir"]
    # Flatten the dictionary to a list of arguments
    args = _flatten_dict_to_args(new_config_dict, ignore_keys)

    return args


def _update_config(config: DictConfig):
    exp_dir = os.path.abspath(config.experiment.exp_dir)
    assert os.path.isdir(exp_dir), f"Directory {exp_dir} does not exist."

    OmegaConf.set_struct(config, False)

    if config.experiment.runner.get("no_shared_fs", False):
        config.train.system.no_shared_fs = True

    config = config.train.system

    if config.get("checkpoint", None) is None:
        config.checkpoint = DictConfig({})
    
    if config.get("logging", None) is None:
        config.logging = DictConfig({})

    ckpt_save_dir = (
        os.path.abspath(config.checkpoint.save)
        if config.checkpoint.get("save", None)
        else os.path.join(exp_dir, "checkpoints")
    )
    ckpt_load_dir = (
        os.path.abspath(config.checkpoint.load)
        if config.checkpoint.get("load", None)
        else os.path.join(exp_dir, "checkpoints")
    )
    wandb_dir = (
        os.path.abspath(config.logging.wandb_save_dir)
        if config.logging.get("wandb_save_dir", None)
        else os.path.join(exp_dir, "wandb")
    )
    tensorboard_dir = (
        os.path.abspath(config.logging.tensorboard_dir)
        if config.logging.get("tensorboard_dir", None)
        else os.path.join(exp_dir, "tensorboard")
    )
    log_dir = (
        os.path.abspath(config.logging.log_dir)
        if config.logging.get("log_dir", None)
        else os.path.join(exp_dir, "logs")
    )
    scripts_dir = os.path.join(log_dir, "scripts")
    pids_dir = os.path.join(log_dir, "pids")
    details_dir = os.path.join(log_dir, "details")

    config.checkpoint.save = ckpt_save_dir
    config.checkpoint.load = ckpt_load_dir
    config.logging.log_dir = log_dir
    config.logging.scripts_dir = scripts_dir
    config.logging.pids_dir = pids_dir
    config.logging.details_dir = details_dir
    config.logging.tensorboard_dir = tensorboard_dir
    config.logging.wandb_save_dir = wandb_dir

    OmegaConf.set_struct(config, True)


def _get_nnodes(nnodes_from_hostfile=None, nnodes_from_args=None):
    assert nnodes_from_hostfile is not None or nnodes_from_args is not None
    if nnodes_from_hostfile is not None and nnodes_from_args is not None:
        if isinstance(nnodes_from_args, str) and ":" in nnodes_from_args:
            # Ignore the max nnodes from the args, no elastic support 
            nnodes_from_args, _ = nnodes_from_args.split(":")
        return min(nnodes_from_hostfile, int(nnodes_from_args))
    elif nnodes_from_hostfile is not None:
        return nnodes_from_hostfile
    elif nnodes_from_args is not None:
        if isinstance(nnodes_from_args, str) and ":" in nnodes_from_args:
            # Ignore the max nnodes from the args, no elastic support 
            nnodes_from_args, _ = nnodes_from_args.split(":")
        return int(nnodes_from_args)


def _get_nproc_per_node(
    nproc_from_hostfile=None, nproc_from_args=None, num_visible_devices=None
):
    if nproc_from_hostfile is not None and nproc_from_args is not None:
        nproc = min(nproc_from_hostfile, int(nproc_from_args))
        if num_visible_devices:
            return min(nproc, num_visible_devices)
        else:
            return nproc
    elif nproc_from_hostfile is not None:
        if num_visible_devices:
            return min(nproc_from_hostfile, num_visible_devices)
        else:
            return nproc_from_hostfile
    elif nproc_from_args is not None:
        if num_visible_devices:
            return min(int(nproc_from_args), num_visible_devices)
        else:
            return nproc_from_args
    else:
        if num_visible_devices:
            return num_visible_devices
        else:
            return 1


def _get_runner_cmd(
    host,
    master_addr,
    master_port,
    nnodes,
    node_rank,
    nproc_per_node,
    config: DictConfig,
):
    runner_config = config.experiment.runner
    logging_config = config.train.system.logging

    if runner_config.get("per_node_task", False):
        nnodes = 1
        node_rank = 0
        master_addr = "localhost"

    rdzv_id = runner_config.get("rdzv_id", "default")
    log_dir = runner_config.get("log_dir", logging_config.details_dir)
    log_dir = os.path.abspath(log_dir)
    no_shared_fs = config.experiment.get("no_shared_fs", False)
    if no_shared_fs:
        log_dir = os.path.join(log_dir, f"host")
    else:
        log_dir = os.path.join(log_dir, f"host_{node_rank}_{host}")
    log_dir = os.path.join(log_dir, datetime.now().strftime("%Y%m%d_%H%M%S.%f"))
    rdzv_backend = runner_config.get("rdzv_backend", "c10d")
    rdzv_endpoint = runner_config.get("rdzv_endpoint", f"{master_addr}:{master_port}")
    redirect = runner_config.get("redirects", "3")
    tee = runner_config.get("tee", "3")
    backend = runner_config.get("backend", "torchrun")

    runner_args = OmegaConf.to_container(runner_config, resolve=True)
    if "type" in runner_args:
        del runner_args["type"]
    if "backend" in runner_args:
        del runner_args["backend"]
    if "per_node_task" in runner_args:
        del runner_args["per_node_task"]
    if "hostfile" in runner_args:
        del runner_args["hostfile"]
    if "master_addr" in runner_args:
        del runner_args["master_addr"]
    if "master_port" in runner_args:
        del runner_args["master_port"]

    runner_args["rdzv_id"] = rdzv_id
    # runner_args["master_addr"] = master_addr
    # runner_args["master_port"] = master_port
    runner_args["nnodes"] = nnodes
    runner_args["node_rank"] = node_rank
    runner_args["nproc_per_node"] = nproc_per_node
    runner_args["rdzv_backend"] = rdzv_backend 
    runner_args["rdzv_endpoint"] = rdzv_endpoint
    runner_args["log_dir"] = (
        log_dir if backend == "torchrun" else os.path.join(log_dir, rdzv_id)
    )
    runner_args["redirects"] = redirect
    runner_args["tee"] = tee

    runner_cmd = [backend]
    for key, value in runner_args.items():
        if isinstance(value, bool):
            if value:
                runner_cmd.append(f"--{key}")
        else:
            runner_cmd.append(f"--{key}")
            runner_cmd.append(f"{value}")
    return runner_cmd


def _generate_run_script(config, host, node_rank, cmd, background=True, with_test=False):
    system_config = config.train.system
    logging_config = config.train.system.logging

    no_shared_fs = config.experiment.runner.get("no_shared_fs", False)
    if no_shared_fs:
        host_output_file = os.path.join(logging_config.log_dir, f"host.output")
    else:
        host_output_file = os.path.join(
            logging_config.log_dir, f"host_{node_rank}_{host}.output"
        )
    host_run_script_file = os.path.join(
        logging_config.scripts_dir, f"host_{node_rank}_{host}_run.sh"
    )
    host_pid_file = os.path.join(
        logging_config.pids_dir, f"host_{node_rank}_{host}.pid"
    )

    os.makedirs(logging_config.scripts_dir, exist_ok=True)

    root_dir = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    megatron_dir = os.path.join(root_dir, "megatron")
    cmds_config = config.experiment.get("cmds", None)
    if cmds_config:
        before_start = cmds_config.get("before_start", "")
    else:
        before_start = ""
    with open(host_run_script_file, "w") as f:
        f.write("#!/bin/bash\n\n")
        f.write(f"{before_start}\n")
        f.write(f"mkdir -p {system_config.checkpoint.load}\n")
        f.write(f"mkdir -p {system_config.checkpoint.save}\n")
        f.write(f"mkdir -p {system_config.logging.log_dir}\n")
        f.write(f"mkdir -p {system_config.logging.pids_dir}\n")
        f.write(f"mkdir -p {system_config.logging.details_dir}\n")
        f.write(f"mkdir -p {system_config.logging.tensorboard_dir}\n")
        f.write(f"mkdir -p {system_config.logging.wandb_save_dir}\n")
        f.write(f"\n")
        f.write(f"cd {root_dir}\n")
        f.write(f"\n")
        f.write(f"export PYTHONPATH={megatron_dir}:{root_dir}\n")
        f.write(f"\n")
        f.write(f'cmd="{cmd}"\n')
        f.write(f"\n")
        if with_test:
            f.write(f'bash -c "$cmd" \n')
        else:
            # TODO: need a option to control whether to append or overwrite the output file
            # Now, it always appends to the output file
            if background:
                f.write(
                    f'nohup bash -c "$cmd" >> {host_output_file} 2>&1 & echo $! > {host_pid_file}\n'
                )
            else:
                f.write(f'bash -c "$cmd" >> {host_output_file} 2>&1\n')
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(host_run_script_file, 0o755)

    return host_run_script_file


def _generate_stop_script(config, host, node_rank):
    logging_config = config.train.system.logging

    host_stop_script_file = os.path.join(
        logging_config.scripts_dir, f"host_{node_rank}_{host}_stop.sh"
    )

    host_pid_file = os.path.join(
        logging_config.pids_dir, f"host_{node_rank}_{host}.pid"
    )

    os.makedirs(logging_config.scripts_dir, exist_ok=True)

    cmds_config = config.experiment.get("cmds", None)
    if cmds_config:
        after_stop = cmds_config.get("after_stop", "")
    else:
        after_stop = ""
    with open(host_stop_script_file, "w") as f:
        f.write("#!/bin/bash\n\n")
        f.write("if [ -f " + host_pid_file + " ]; then\n")
        f.write("    pid=$(cat " + host_pid_file + ")\n")
        f.write("    pkill -P $pid\n")
        f.write("else\n")
        # TODO: This is a temporary fix. We need to find a better way to stop the job.
        f.write("    pkill -f 'torchrun'\n")
        f.write("fi\n")
        f.write(f"{after_stop}\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(host_stop_script_file, 0o755)

    return host_stop_script_file


class MultiNodeRunner(ABC):

    @abstractmethod
    def run(self):
        """Run the command"""

    @abstractmethod
    def stop(self):
        """stop the command"""


class SSHRunner(MultiNodeRunner):
    def __init__(self, config: DictConfig):
        self.config = config
        _update_config(self.config)
        self.resources = parse_hostfile(self.config.experiment.runner.get("hostfile", None))

    def _prepare(self):
        self.rdzv_id = datetime.now().strftime("%Y%m%d_%H%M%S.%f")
        self.user_envs = self.config.experiment.get("envs", {})
        self.user_script = self.config.experiment.task.entrypoint
        if self.config.experiment.task.type == "train":
            self.user_args = get_megatron_args(self.config)
        else:
            raise ValueError(f"Unsupported task type: {self.config.experiment.task.type}")

    def _run_each(
        self,
        host,
        master_addr,
        master_port,
        nnodes,
        node_rank,
        nproc_per_node,
        with_test=False,
        dryrun=False,
    ):
        export_cmd = []
        for k, v in self.user_envs.items():
            export_cmd += [f"{k}={v}"]

        runner_cmd = _get_runner_cmd(
            host,
            master_addr,
            master_port,
            nnodes,
            node_rank,
            nproc_per_node,
            self.config,
        )

        cmd = shlex.join(export_cmd + runner_cmd + [self.user_script] + self.user_args)

        host_run_script_file = _generate_run_script(
            self.config, host, node_rank, cmd, background=True, with_test=with_test
        )

        logging_config = self.config.train.system.logging
        if host != "localhost":
            ssh_port = self.config.experiment.runner.get("ssh_port", 22)
            # Step 1: make sure the scripts_dir exists on the remote host
            run_ssh_command(host, f"mkdir -p {logging_config.scripts_dir}", ssh_port, dryrun)

            # Step 2: copy the host_run_script_file to the remote host
            no_shared_fs = self.config.experiment.runner.get("no_shared_fs", False)
            if no_shared_fs:
                run_scp_command(
                    host, host_run_script_file, logging_config.scripts_dir, ssh_port, dryrun
                )

            # Step 3: run the host_run_script_file on the remote host
            run_ssh_command(host, f"bash {host_run_script_file}", ssh_port, dryrun)
        else:
            run_local_command(f"bash {host_run_script_file}", dryrun)

    def run(self, with_test=False, dryrun=False):
        self._prepare()
        logger.info("\n************** configuration ***********")
        logger.info(f"\n{OmegaConf.to_yaml(self.config)}")

        num_visible_devices = None
        visible_devices = self.user_envs.get("CUDA_VISIBLE_DEVICES", None)
        if visible_devices:
            visible_devices = visible_devices.split(",")
            num_visible_devices = len(visible_devices)

        runner_config = self.config.experiment.runner

        # If hostfile is provided, use the resources from the hostfile
        if self.resources is not None:
            nnodes_from_hostfile = len(self.resources.keys())
            nnodes_from_args = runner_config.get("nnodes", None)
            nnodes = _get_nnodes(nnodes_from_hostfile, nnodes_from_args)
            avaliable_ip = list(self.resources.keys())[0]
            avalibale_port = get_free_port()
            for node_rank, (host, resource_info) in enumerate(self.resources.items()):
                if node_rank >= nnodes:
                    break
                nproc_from_hostfile = resource_info["slots"]
                nproc_from_args = runner_config.get("nproc_per_node", None)
                nproc_per_node = _get_nproc_per_node(
                    nproc_from_hostfile, nproc_from_args, num_visible_devices
                )
                master_addr = runner_config.get("master_addr", avaliable_ip)
                master_port = runner_config.get("master_port", avalibale_port)
                self._run_each(
                    host,
                    master_addr,
                    master_port,
                    nnodes,
                    node_rank,
                    nproc_per_node,
                    with_test=with_test,
                    dryrun=dryrun,
                )
        else:
            # If hostfile is not provided, run the job on localhost
            nproc_from_args = runner_config.get("nproc_per_node", None)
            nproc_per_node = _get_nproc_per_node(None, nproc_from_args, num_visible_devices)
            avaliable_addr = runner_config.get("master_addr", "localhost")
            avaliable_port = runner_config.get("master_port", get_free_port())
            self._run_each(
                "localhost",
                avaliable_addr,
                avaliable_port,
                1,
                0,
                nproc_per_node,
                with_test=with_test,
                dryrun=dryrun,
            )

    def _stop_each(self, host, node_rank):
        host_stop_script_file = _generate_stop_script(self.config, host, node_rank)
        logging_config = self.config.train.system.logging

        if host != "localhost":
            ssh_port = self.config.experiment.runner.get("ssh_port", 22)
            # Step 1: make sure the scripts_dir exists on the remote host
            run_ssh_command(host, f"mkdir -p {logging_config.scripts_dir}", ssh_port)
            # Step 2: copy the host_run_script_file to the remote host
            no_shared_fs = self.config.experiment.runner.get("no_shared_fs", False)
            if no_shared_fs:
                run_scp_command(
                    host, host_stop_script_file, logging_config.scripts_dir, ssh_port
                )
            # Step 3: run the host_run_script_file on the remote host
            run_ssh_command(host, f"bash {host_stop_script_file}", ssh_port)
        else:
            run_local_command(f"bash {host_stop_script_file}")

    def stop(self):
        if self.resources is None:
            self._stop_each("localhost", 0)
            return

        nnodes = _get_nnodes(
            len(self.resources), self.config.experiment.runner.get("nnodes", None)
        )

        for node_rank, (host, _) in enumerate(self.resources.items()):
            if node_rank >= nnodes:
                break
            self._stop_each(host, node_rank)


class CloudRunner(MultiNodeRunner):

    def __init__(self, config: DictConfig):
        self.config = config
        _update_config(self.config)

    def _prepare(self):
        self.user_envs = self.config.experiment.get("envs", {})
        self.user_script = self.config.experiment.task.entrypoint
        if self.config.experiment.task.type == "train":
            self.user_args = get_megatron_args(self.config)
        else:
            raise ValueError(f"Unsupported task type: {self.config.experiment.task.type}")

    def _run_each(
        self,
        host,
        master_addr,
        master_port,
        nnodes,
        node_rank,
        nproc_per_node,
        with_test=False,
        dryrun=False,
    ):
        export_cmd = []
        for k, v in self.user_envs.items():
            export_cmd += [f"{k}={v}"]

        runner_cmd = _get_runner_cmd(
            host,
            master_addr,
            master_port,
            nnodes,
            node_rank,
            nproc_per_node,
            self.config,
        )

        cmd = shlex.join(export_cmd + runner_cmd + [self.user_script] + self.user_args)

        host_run_script_file = _generate_run_script(
            self.config, host, node_rank, cmd, background=False, with_test=with_test
        )

        run_local_command(f"bash {host_run_script_file}", dryrun)

    def run(self, with_test=False, dryrun=False):
        self._prepare()
        logger.info("\n************** configuration ***********")
        logger.info(f"\n{OmegaConf.to_yaml(self.config)}")
        if dryrun:
            logger.info("Dryrun mode is not supported in CloudRunner.")
            return

        num_visible_devices = None
        visible_devices = self.user_envs.get("CUDA_VISIBLE_DEVICES", None)
        if visible_devices:
            visible_devices = visible_devices.split(",")
            num_visible_devices = len(visible_devices)

        runner_config = self.config.experiment.runner
        nnodes_from_args = runner_config.get("nnodes", None)
        nnodes = _get_nnodes(None, nnodes_from_args)
        node_rank = runner_config.node_rank
        nproc_from_args = runner_config.get("nproc_per_node", None)
        nproc_per_node = _get_nproc_per_node(
            None, nproc_from_args, num_visible_devices
        )
        master_addr = runner_config.master_addr
        master_port = runner_config.master_port
        host = get_host_name_or_ip() 
        self._run_each(
            host,
            master_addr,
            master_port,
            nnodes,
            node_rank,
            nproc_per_node,
            with_test=with_test,
            dryrun=dryrun,
        )
