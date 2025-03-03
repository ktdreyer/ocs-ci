import copy
import logging
import re
from prettytable import PrettyTable
from collections import defaultdict

from subprocess import TimeoutExpired

from ocs_ci.ocs.machine import get_machine_objs

from ocs_ci.framework import config
from ocs_ci.ocs.exceptions import TimeoutExpiredError
from ocs_ci.ocs.ocp import OCP
from ocs_ci.ocs.resources.ocs import OCS
from ocs_ci.ocs import constants, exceptions, ocp
from ocs_ci.utility.utils import TimeoutSampler, convert_device_size
from ocs_ci.ocs import machine
from ocs_ci.ocs.resources import pod
from ocs_ci.utility.utils import set_selinux_permissions

log = logging.getLogger(__name__)


def get_node_objs(node_names=None):
    """
    Get node objects by node names

    Args:
        node_names (list): The node names to get their objects for.
            If None, will return all cluster nodes

    Returns:
        list: Cluster node OCP objects

    """
    nodes_obj = OCP(kind="node")
    node_dicts = nodes_obj.get()["items"]
    if not node_names:
        nodes = [OCS(**node_obj) for node_obj in node_dicts]
    else:
        nodes = [
            OCS(**node_obj)
            for node_obj in node_dicts
            if (node_obj.get("metadata").get("name") in node_names)
        ]
    assert nodes, "Failed to get the nodes OCS objects"
    return nodes


def get_nodes(node_type=constants.WORKER_MACHINE, num_of_nodes=None):
    """
    Get cluster's nodes according to the node type (e.g. worker, master) and the
    number of requested nodes from that type

    Args:
        node_type (str): The node type (e.g. worker, master)
        num_of_nodes (int): The number of nodes to be returned

    Returns:
        list: The nodes OCP instances

    """
    typed_nodes = [
        node
        for node in get_node_objs()
        if node_type in node.ocp.get_resource(resource_name=node.name, column="ROLES")
    ]
    if num_of_nodes:
        typed_nodes = typed_nodes[:num_of_nodes]
    return typed_nodes


def get_all_nodes():
    """
    Gets the all nodes in cluster

    Returns:
        list: List of node name

    """
    ocp_node_obj = ocp.OCP(kind=constants.NODE)
    node_items = ocp_node_obj.get().get("items")
    return [node["metadata"]["name"] for node in node_items]


def wait_for_nodes_status(node_names=None, status=constants.NODE_READY, timeout=180):
    """
    Wait until all nodes are in the given status

    Args:
        node_names (list): The node names to wait for to reached the desired state
            If None, will wait for all cluster nodes
        status (str): The node status to wait for
            (e.g. 'Ready', 'NotReady', 'SchedulingDisabled')
        timeout (int): The number in seconds to wait for the nodes to reach
            the status

    Raises:
        ResourceWrongStatusException: In case one or more nodes haven't
            reached the desired state

    """
    try:
        if not node_names:
            for sample in TimeoutSampler(60, 3, get_node_objs):
                if sample:
                    node_names = [node.name for node in sample]
                    break
        nodes_not_in_state = copy.deepcopy(node_names)
        log.info(f"Waiting for nodes {node_names} to reach status {status}")
        for sample in TimeoutSampler(timeout, 3, get_node_objs, nodes_not_in_state):
            for node in sample:
                if node.ocp.get_resource_status(node.name) == status:
                    log.info(f"Node {node.name} reached status {status}")
                    nodes_not_in_state.remove(node.name)
            if not nodes_not_in_state:
                break
        log.info(f"The following nodes reached status {status}: {node_names}")
    except TimeoutExpiredError:
        log.error(
            f"The following nodes haven't reached status {status}: "
            f"{nodes_not_in_state}"
        )
        raise exceptions.ResourceWrongStatusException(
            node_names, [n.describe() for n in get_node_objs(node_names)]
        )


def unschedule_nodes(node_names):
    """
    Change nodes to be unscheduled

    Args:
        node_names (list): The names of the nodes

    """
    ocp = OCP(kind="node")
    node_names_str = " ".join(node_names)
    log.info(f"Unscheduling nodes {node_names_str}")
    ocp.exec_oc_cmd(f"adm cordon {node_names_str}")

    wait_for_nodes_status(node_names, status=constants.NODE_READY_SCHEDULING_DISABLED)


def schedule_nodes(node_names):
    """
    Change nodes to be scheduled

    Args:
        node_names (list): The names of the nodes

    """
    ocp = OCP(kind="node")
    node_names_str = " ".join(node_names)
    ocp.exec_oc_cmd(f"adm uncordon {node_names_str}")
    log.info(f"Scheduling nodes {node_names_str}")
    wait_for_nodes_status(node_names)


def drain_nodes(node_names):
    """
    Drain nodes

    Args:
        node_names (list): The names of the nodes

    Raises:
        TimeoutExpired: in case drain command fails to complete in time

    """
    ocp = OCP(kind="node")
    node_names_str = " ".join(node_names)
    log.info(f"Draining nodes {node_names_str}")
    try:
        ocp.exec_oc_cmd(
            f"adm drain {node_names_str} --force=true --ignore-daemonsets "
            f"--delete-local-data",
            timeout=1800,
        )
    except TimeoutExpired:
        ct_pod = pod.get_ceph_tools_pod()
        ceph_status = ct_pod.exec_cmd_on_pod("ceph status", out_yaml_format=False)
        log.error(f"Drain command failed to complete. Ceph status: {ceph_status}")
        # TODO: Add re-balance status once pull/1679 is merged
        raise


def get_typed_worker_nodes(os_id="rhcos"):
    """
    Get worker nodes with specific OS

    Args:
        os_id (str): OS type like rhcos, RHEL etc...

    Returns:
        list: list of worker nodes instances having specified os

    """
    worker_nodes = get_nodes(node_type="worker")
    return [
        node
        for node in worker_nodes
        if node.get().get("metadata").get("labels").get("node.openshift.io/os_id")
        == os_id
    ]


def remove_nodes(nodes):
    """
    Remove the nodes from cluster

    Args:
        nodes (list): list of node instances to remove from cluster

    """
    ocp = OCP(kind="node")
    node_names = [node.get().get("metadata").get("name") for node in nodes]
    node_names_str = " ".join(node_names)

    # unschedule node
    unschedule_nodes(node_names)

    # Drain all the pods from the node
    drain_nodes(node_names)

    # delete the nodes
    log.info(f"Deleting nodes {node_names_str}")
    ocp.exec_oc_cmd(f"delete nodes {node_names_str}")


def get_node_ips(node_type="worker"):
    """
    Gets the node public IP

    Args:
        node_type (str): The node type (e.g. worker, master)

    Returns:
        list: Node IP's

    """
    ocp = OCP(kind=constants.NODE)
    if node_type == "worker":
        nodes = ocp.get(selector=constants.WORKER_LABEL).get("items")
    if node_type == "master:":
        nodes = ocp.get(selector=constants.MASTER_LABEL).get("items")

    if config.ENV_DATA["platform"].lower() == constants.AWS_PLATFORM:
        raise NotImplementedError
    elif config.ENV_DATA["platform"].lower() == constants.VSPHERE_PLATFORM:
        return [
            each["address"]
            for node in nodes
            for each in node["status"]["addresses"]
            if each["type"] == "ExternalIP"
        ]
    else:
        raise NotImplementedError


def add_new_node_and_label_it(machineset_name, num_nodes=1, mark_for_ocs_label=True):
    """
    Add a new node for ipi and label it

    Args:
        machineset_name (str): Name of the machine set
        num_nodes (int): number of nodes to add
        mark_for_ocs_label (bool): True if label the new node
    eg: add_new_node_and_label_it("new-tdesala-zlqzn-worker-us-east-2a")

    Returns:
        list: new spun node names

    """
    # Get the initial nodes list
    initial_nodes = get_worker_nodes()
    log.info(f"Current available worker nodes are {initial_nodes}")

    # get machineset replica count
    machineset_replica_count = machine.get_replica_count(machineset_name)
    log.info(f"{machineset_name} has replica count: {machineset_replica_count}")

    # Increase its replica count
    log.info(f"Increasing the replica count by {num_nodes}")
    machine.add_node(machineset_name, count=machineset_replica_count + num_nodes)
    log.info(
        f"{machineset_name} now has replica "
        f"count: {machineset_replica_count + num_nodes}"
    )

    # wait for the new node to come to ready state
    log.info("Waiting for the new node to be in ready state")
    machine.wait_for_new_node_to_be_ready(machineset_name)

    # Get the node name of new spun node
    nodes_after_new_spun_node = get_worker_nodes()
    new_spun_nodes = list(set(nodes_after_new_spun_node) - set(initial_nodes))
    log.info(f"New spun nodes: {new_spun_nodes}")

    # Label it
    if mark_for_ocs_label:
        node_obj = ocp.OCP(kind="node")
        for new_spun_node in new_spun_nodes:
            if is_node_labeled(new_spun_node):
                logging.info(
                    f"node {new_spun_node} is already labeled with the OCS storage label"
                )
            else:
                node_obj.add_label(
                    resource_name=new_spun_node, label=constants.OPERATOR_NODE_LABEL
                )
                logging.info(
                    f"Successfully labeled {new_spun_node} with OCS storage label"
                )

    return new_spun_nodes


def add_new_node_and_label_upi(
    node_type, num_nodes, mark_for_ocs_label=True, node_conf=None
):
    """
    Add a new node for aws/vmware upi platform and label it

    Args:
        node_type (str): Type of node, RHEL or RHCOS
        num_nodes (int): number of nodes to add
        mark_for_ocs_label (bool): True if label the new node
        node_conf (dict): The node configurations.

    Returns:
        list: new spun node names

    """
    node_conf = node_conf or {}
    initial_nodes = get_worker_nodes()
    from ocs_ci.ocs.platform_nodes import PlatformNodesFactory

    plt = PlatformNodesFactory()
    node_util = plt.get_nodes_platform()
    node_util.create_and_attach_nodes_to_cluster(node_conf, node_type, num_nodes)
    for sample in TimeoutSampler(timeout=600, sleep=6, func=get_worker_nodes):
        if len(sample) == len(initial_nodes) + num_nodes:
            break

    nodes_after_exp = get_worker_nodes()
    wait_for_nodes_status(node_names=get_worker_nodes(), status=constants.NODE_READY)

    new_spun_nodes = list(set(nodes_after_exp) - set(initial_nodes))
    log.info(f"New spun nodes: {new_spun_nodes}")
    if node_type == constants.RHEL_OS:
        set_selinux_permissions(workers=new_spun_nodes)

    if mark_for_ocs_label:
        node_obj = ocp.OCP(kind="node")
        for new_spun_node in new_spun_nodes:
            node_obj.add_label(
                resource_name=new_spun_node, label=constants.OPERATOR_NODE_LABEL
            )
            logging.info(f"Successfully labeled {new_spun_node} with OCS storage label")
    return new_spun_nodes


def get_node_logs(node_name):
    """
    Get logs from a given node

    pod_name (str): Name of the node

    Returns:
        str: Output of 'dmesg' run on node
    """
    node = OCP(kind="node")
    return node.exec_oc_debug_cmd(node_name, ["dmesg"])


def get_node_resource_utilization_from_adm_top(
    nodename=None, node_type=constants.WORKER_MACHINE, print_table=False
):
    """
    Gets the node's cpu and memory utilization in percentage using adm top command.

    Args:
        nodename (str) : The node name
        node_type (str) : The node type (e.g. master, worker)

    Returns:
        dict : Node name and its cpu and memory utilization in
               percentage

    """

    node_names = (
        [nodename]
        if nodename
        else [node.name for node in get_nodes(node_type=node_type)]
    )

    # Validate node is in Ready state
    wait_for_nodes_status(node_names, status=constants.NODE_READY, timeout=30)

    obj = ocp.OCP()
    resource_utilization_all_nodes = obj.exec_oc_cmd(
        command="adm top nodes", out_yaml_format=False
    ).split("\n")
    utilization_dict = {}

    for node in node_names:
        for value in resource_utilization_all_nodes:
            if node in value:
                value = re.findall(r"(\d{1,3})%", value.strip())
                cpu_utilization = value[0]
                log.info(
                    "The CPU utilized by the node " f"{node} is {cpu_utilization}%"
                )
                memory_utilization = value[1]
                log.info(
                    "The memory utilized of the node "
                    f"{node} is {memory_utilization}%"
                )
                utilization_dict[node] = {
                    "cpu": int(cpu_utilization),
                    "memory": int(memory_utilization),
                }

    if print_table:
        print_table_node_resource_utilization(
            utilization_dict=utilization_dict,
            field_names=["Node Name", "CPU USAGE adm_top", "Memory USAGE adm_top"],
        )
    return utilization_dict


def get_node_resource_utilization_from_oc_describe(
    nodename=None, node_type=constants.WORKER_MACHINE, print_table=False
):
    """
    Gets the node's cpu and memory utilization in percentage using oc describe node

    Args:
        nodename (str) : The node name
        node_type (str) : The node type (e.g. master, worker)

    Returns:
        dict : Node name and its cpu and memory utilization in
               percentage

    """

    node_names = (
        [nodename]
        if nodename
        else [node.name for node in get_nodes(node_type=node_type)]
    )
    obj = ocp.OCP()
    utilization_dict = {}
    for node in node_names:
        output = obj.exec_oc_cmd(
            command=f"describe node {node}", out_yaml_format=False
        ).split("\n")
        for line in output:
            if "cpu  " in line:
                cpu_data = line.split(" ")
                cpu = re.findall(r"\d+", [i for i in cpu_data if i][2])
            if "memory  " in line:
                mem_data = line.split(" ")
                mem = re.findall(r"\d+", [i for i in mem_data if i][2])
        utilization_dict[node] = {"cpu": int(cpu[0]), "memory": int(mem[0])}

    if print_table:
        print_table_node_resource_utilization(
            utilization_dict=utilization_dict,
            field_names=[
                "Node Name",
                "CPU USAGE oc_describe",
                "Memory USAGE oc_describe",
            ],
        )

    return utilization_dict


def get_running_pod_count_from_node(nodename=None, node_type=constants.WORKER_MACHINE):
    """
    Gets the node running pod count using oc describe node

    Args:
        nodename (str) : The node name
        node_type (str) : The node type (e.g. master, worker)

    Returns:
        dict : Node name and its pod_count

    """

    node_names = (
        [nodename]
        if nodename
        else [node.name for node in get_nodes(node_type=node_type)]
    )
    obj = ocp.OCP()
    pod_count_dict = {}
    for node in node_names:
        output = obj.exec_oc_cmd(
            command=f"describe node {node}", out_yaml_format=False
        ).split("\n")
        for line in output:
            if "Non-terminated Pods:  " in line:
                count_line = line.split(" ")
                pod_count = re.findall(r"\d+", [i for i in count_line if i][2])
        pod_count_dict[node] = int(pod_count[0])

    return pod_count_dict


def print_table_node_resource_utilization(utilization_dict, field_names):
    """
    Print table of node utilization

    Args:
        utilization_dict (dict) : CPU and Memory utilization per Node
        field_names (list) : The field names of the table

    """
    usage_memory_table = PrettyTable()
    usage_memory_table.field_names = field_names
    for node, util_node in utilization_dict.items():
        usage_memory_table.add_row(
            [node, f'{util_node["cpu"]}%', f'{util_node["memory"]}%']
        )
    log.info(f"\n{usage_memory_table}\n")


def node_network_failure(node_names, wait=True):
    """
    Induce node network failure
    Bring node network interface down, making the node unresponsive

    Args:
        node_names (list): The names of the nodes
        wait (bool): True in case wait for status is needed, False otherwise

    Returns:
        bool: True if node network fail is successful
    """
    if not isinstance(node_names, list):
        node_names = [node_names]

    ocp = OCP(kind="node")
    fail_nw_cmd = "ifconfig $(route | grep default | awk '{print $(NF)}') down"

    for node_name in node_names:
        try:
            ocp.exec_oc_debug_cmd(node=node_name, cmd_list=[fail_nw_cmd], timeout=15)
        except TimeoutExpired:
            pass

    if wait:
        wait_for_nodes_status(node_names=node_names, status=constants.NODE_NOT_READY)
    return True


def get_osd_running_nodes():
    """
    Gets the osd running node names

    Returns:
        list: OSD node names

    """
    return [pod.get_pod_node(osd_node).name for osd_node in pod.get_osd_pods()]


def get_osds_per_node():
    """
    Gets the osd running pod names per node name

    Returns:
        dict: {"Node name":["osd running pod name running on the node",..,]}

    """
    dic_node_osd = defaultdict(list)
    for osd_pod in pod.get_osd_pods():
        dic_node_osd[pod.get_pod_node(osd_pod).name].append(osd_pod.name)
    return dic_node_osd


def get_app_pod_running_nodes(pod_obj):
    """
    Gets the app pod running node names

    Args:
        pod_obj (list): List of app pod objects

    Returns:
        list: App pod running node names

    """
    return [pod.get_pod_node(obj_pod).name for obj_pod in pod_obj]


def get_both_osd_and_app_pod_running_node(osd_running_nodes, app_pod_running_nodes):
    """
    Gets both osd and app pod running node names

    Args:
        osd_running_nodes(list): List of osd running node names
        app_pod_running_nodes(list): List of app pod running node names

    Returns:
        list: Both OSD and app pod running node names

    """
    common_nodes = list(set(osd_running_nodes) & set(app_pod_running_nodes))
    log.info(f"Common node is {common_nodes}")
    return common_nodes


def get_node_from_machine_name(machine_name):
    """
    Get node name from a given machine_name.

    Args:
        machine_name (str): Name of Machine

    Returns:
        str: Name of Node (or None if not found)

    """
    machine_objs = get_machine_objs()
    for machine_obj in machine_objs:
        if machine_obj.name == machine_name:
            machine_dict = machine_obj.get()
            node_name = machine_dict["status"]["nodeRef"]["name"]
            return node_name


def get_provider():
    """
    Return the OCP Provider (Platform)

    Returns:
         str: The Provider that the OCP is running on

    """

    ocp_cluster = OCP(kind="", resource_name="nodes")
    results = ocp_cluster.get("nodes")["items"][0]["spec"]
    if "providerID" in results:
        return results["providerID"].split(":")[0]
    else:
        return "BareMetal"


def get_compute_node_names(no_replace=False):
    """
    Gets the compute node names

    Args:
        no_replace (bool): If False '.' will replaced with '-'

    Returns:
        list: List of compute node names

    """
    platform = config.ENV_DATA.get("platform").lower()
    compute_node_objs = get_nodes()
    if platform in [constants.VSPHERE_PLATFORM, constants.AWS_PLATFORM]:
        return [
            compute_obj.get()["metadata"]["labels"][constants.HOSTNAME_LABEL]
            for compute_obj in compute_node_objs
        ]
    elif platform in [
        constants.BAREMETAL_PLATFORM,
        constants.BAREMETALPSI_PLATFORM,
        constants.IBM_POWER_PLATFORM,
    ]:
        if no_replace:
            return [
                compute_obj.get()["metadata"]["labels"][constants.HOSTNAME_LABEL]
                for compute_obj in compute_node_objs
            ]
        else:
            return [
                compute_obj.get()["metadata"]["labels"][
                    constants.HOSTNAME_LABEL
                ].replace(".", "-")
                for compute_obj in compute_node_objs
            ]
    else:
        raise NotImplementedError


def get_ocs_nodes(num_of_nodes=None):
    """
    Gets the ocs nodes

    Args:
        num_of_nodes (int): The number of ocs nodes to return. If not specified,
            it returns all the ocs nodes.

    Returns:
        list: List of ocs nodes

    """
    ocs_node_names = machine.get_labeled_nodes(constants.OPERATOR_NODE_LABEL)
    ocs_nodes = get_node_objs(ocs_node_names)
    num_of_nodes = num_of_nodes or len(ocs_nodes)

    return ocs_nodes[:num_of_nodes]


def get_node_name(node_obj):
    """
    Get oc node's name

    Args:
        node_obj (node_obj): oc node object

    Returns:
        str: node's name

    """
    node_items = node_obj.get("items")
    return node_items["metadata"]["name"]


def check_nodes_specs(min_memory, min_cpu):
    """
    Check that the cluster worker nodes meet the required minimum CPU and memory

    Args:
        min_memory (int): The required minimum memory in bytes
        min_cpu (int): The required minimum number of vCPUs

    Returns:
        bool: True if all nodes meet the required minimum specs, False otherwise

    """
    nodes = get_nodes()
    log.info(
        f"Checking following nodes with worker selector (assuming that "
        f"this is ran in CI and there are no worker nodes without OCS):\n"
        f"{[node.get().get('metadata').get('name') for node in nodes]}"
    )
    for node in nodes:
        real_cpu = int(node.get()["status"]["capacity"]["cpu"])
        real_memory = convert_device_size(
            node.get()["status"]["capacity"]["memory"], "B"
        )
        if real_cpu < min_cpu or real_memory < min_memory:
            log.warning(
                f"Node {node.get().get('metadata').get('name')} specs don't meet "
                f" the minimum required specs.\n The requirements are: "
                f"{min_cpu} CPUs and {min_memory} Memory\nThe node has: {real_cpu} "
                f"CPUs and {real_memory} Memory"
            )
            return False
    log.info(
        f"Cluster worker nodes meet the minimum requirements of "
        f"{min_cpu} CPUs and {min_memory} Memory"
    )
    return True


def delete_and_create_osd_node_ipi(osd_node_name):
    """
    Unschedule, drain and delete osd node, and creating a new osd node.
    At the end of the function there should be the same number of osd nodes as
    it was in the beginning, and also ceph health should be OK.

    This function is for any IPI platform.

    Args:
        osd_node_name (str): the name of the osd node

    Returns:
        str: The new node name

    """
    log.info("Going to unschedule, drain and delete %s node", osd_node_name)
    # Unscheduling node
    unschedule_nodes([osd_node_name])
    # Draining Node
    drain_nodes([osd_node_name])
    log.info("Getting machine name from specified node name")
    machine_name = machine.get_machine_from_node_name(osd_node_name)
    log.info(f"Node {osd_node_name} associated machine is {machine_name}")
    log.info(f"Deleting machine {machine_name} and waiting for new machine to come up")
    machine.delete_machine_and_check_state_of_new_spinned_machine(machine_name)
    new_machine_list = machine.get_machines()
    for machines in new_machine_list:
        # Trimming is done to get just machine name
        # eg:- machine_name:- prsurve-40-ocs-43-kbrvf-worker-us-east-2b-nlgkr
        # After trimming:- prsurve-40-ocs-43-kbrvf-worker-us-east-2b
        if re.match(machines.name[:-6], machine_name):
            new_machine_name = machines.name
    machineset_name = machine.get_machineset_from_machine_name(new_machine_name)
    log.info("Waiting for new worker node to be in ready state")
    machine.wait_for_new_node_to_be_ready(machineset_name)
    new_node_name = get_node_from_machine_name(new_machine_name)
    log.info("Adding ocs label to newly created worker node")
    node_obj = ocp.OCP(kind="node")
    node_obj.add_label(resource_name=new_node_name, label=constants.OPERATOR_NODE_LABEL)
    log.info(f"Successfully labeled {new_node_name} with OCS storage label")

    return new_node_name


def delete_and_create_osd_node_aws_upi(osd_node_name):
    """
    Unschedule, drain and delete osd node, and creating a new osd node.
    At the end of the function there should be the same number of osd nodes as
    it was in the beginning, and also ceph health should be OK.
    This function is for AWS UPI.

    Args:
        osd_node_name (str): the name of the osd node

    Returns:
        str: The new node name

    """

    osd_node = get_node_objs(node_names=[osd_node_name])[0]
    az = get_node_az(osd_node)
    from ocs_ci.ocs.platform_nodes import AWSNodes

    aws_nodes = AWSNodes()
    stack_name_of_deleted_node = aws_nodes.get_stack_name_of_node(osd_node_name)

    remove_nodes([osd_node])

    log.info(f"name of deleted node = {osd_node_name}")
    log.info(f"availability zone of deleted node = {az}")
    log.info(f"stack name of deleted node = {stack_name_of_deleted_node}")

    if config.ENV_DATA.get("rhel_workers"):
        node_type = constants.RHEL_OS
    else:
        node_type = constants.RHCOS

    log.info("Preparing to create a new node...")
    node_conf = {"stack_name": stack_name_of_deleted_node}
    new_node_names = add_new_node_and_label_upi(node_type, 1, node_conf=node_conf)

    return new_node_names[0]


def get_node_az(node):
    """
    Get the node availability zone

    Args:
        node (ocs_ci.ocs.resources.ocs.OCS): The node object

    Returns:
        str: The name of the node availability zone

    """
    labels = node.get().get("metadata", {}).get("labels", {})
    return labels.get("topology.kubernetes.io/zone")


def delete_and_create_osd_node_vsphere_upi(osd_node_name, use_existing_node=False):
    """
    Unschedule, drain and delete osd node, and creating a new osd node.
    At the end of the function there should be the same number of osd nodes as
    it was in the beginning, and also ceph health should be OK.
    This function is for vSphere UPI.

    Args:
        osd_node_name (str): the name of the osd node
        use_existing_node (bool): If False, create a new node and label it.
            If True, use an existing node to replace the deleted node
            and label it.

    Returns:
        str: The new node name

    """

    osd_node = get_node_objs(node_names=[osd_node_name])[0]
    remove_nodes([osd_node])

    log.info(f"name of deleted node = {osd_node_name}")

    if config.ENV_DATA.get("rhel_workers"):
        node_type = constants.RHEL_OS
    else:
        node_type = constants.RHCOS

    if not use_existing_node:
        log.info("Preparing to create a new node...")
        new_node_names = add_new_node_and_label_upi(node_type, 1)
        new_node_name = new_node_names[0]
    else:
        node_not_in_ocs = get_worker_nodes_not_in_ocs()[0]
        log.info(
            f"Preparing to replace the node {osd_node_name} "
            f"with an existing node {node_not_in_ocs.name}"
        )
        if node_type == constants.RHEL_OS:
            set_selinux_permissions(workers=[node_not_in_ocs])
        label_nodes([node_not_in_ocs])
        new_node_name = node_not_in_ocs.name

    return new_node_name


def label_nodes(nodes, label=constants.OPERATOR_NODE_LABEL):
    """
    Label nodes

    Args:
        nodes (list): list of node objects need to label
        label (str): New label to be assigned for these nodes.
            Default value is the OCS label

    """
    node_obj = ocp.OCP(kind="node")
    for new_node_to_label in nodes:
        node_obj.add_label(resource_name=new_node_to_label.name, label=label)
        logging.info(
            f"Successfully labeled {new_node_to_label.name} " f"with OCS storage label"
        )


def get_master_nodes():
    """
    Fetches all master nodes.

    Returns:
        list: List of names of master nodes

    """
    label = "node-role.kubernetes.io/master"
    ocp_node_obj = ocp.OCP(kind=constants.NODE)
    nodes = ocp_node_obj.get(selector=label).get("items")
    master_nodes_list = [node.get("metadata").get("name") for node in nodes]
    return master_nodes_list


def get_worker_nodes():
    """
    Fetches all worker nodes.

    Returns:
        list: List of names of worker nodes

    """
    label = "node-role.kubernetes.io/worker"
    ocp_node_obj = ocp.OCP(kind=constants.NODE)
    nodes = ocp_node_obj.get(selector=label).get("items")
    # Eliminate infra nodes from worker nodes in case of openshift dedicated
    if config.ENV_DATA["platform"].lower() == "openshiftdedicated":
        infra_nodes = ocp_node_obj.get(selector=constants.INFRA_NODE_LABEL).get("items")
        infra_node_ids = [
            infra_node.get("metadata").get("name") for infra_node in infra_nodes
        ]
        nodes = [
            node
            for node in nodes
            if node.get("metadata").get("name") not in infra_node_ids
        ]
    worker_nodes_list = [node.get("metadata").get("name") for node in nodes]
    return worker_nodes_list


def get_worker_nodes_not_in_ocs():
    """
    Get the worker nodes that are not ocs labeled.

    Returns:
          list: list of worker node objects that are not ocs labeled

    """
    ocs_nodes = get_ocs_nodes()
    ocs_node_names = [n.name for n in ocs_nodes]
    worker_nodes = get_nodes(constants.WORKER_MACHINE)
    return [n for n in worker_nodes if n.name not in ocs_node_names]


def node_replacement_verification_steps_user_side(old_node_name, new_node_name):
    """
    Check the verification steps that the user should perform after the process
    of node replacement as described in the docs

    Args:
        old_node_name (str): The name of the old node that has been deleted
        new_node_name (str): The name of the new node that has been created

    Returns:
        bool: True if all the verification steps passed. False otherwise

    """
    ocs_nodes = get_ocs_nodes()
    ocs_node_names = [n.name for n in ocs_nodes]
    if new_node_name not in ocs_node_names:
        log.warning("The new node not found in ocs nodes")
        return False
    if old_node_name in ocs_node_names:
        log.warning("The old node name found in ocs nodes")
        return False

    csi_cephfsplugin_pods = pod.get_plugin_pods(interface=constants.CEPHFILESYSTEM)
    csi_rbdplugin_pods = pod.get_plugin_pods(interface=constants.CEPHBLOCKPOOL)
    csi_plugin_pods = csi_cephfsplugin_pods + csi_rbdplugin_pods
    if not all([p.status() == constants.STATUS_RUNNING for p in csi_plugin_pods]):
        log.warning("Not all csi rbd and cephfs plugin pods in status running")
        return False

    # It can take some time until all the ocs pods are up and running
    # after the process of node replacement
    if not pod.wait_for_pods_to_be_running():
        log.warning("Not all the pods in running state")
        return False

    log.info("Verification steps from the user side finish successfully")
    return True


def node_replacement_verification_steps_ceph_side(old_node_name, new_node_name):
    """
    Check the verification steps from the Ceph side, after the process
    of node replacement as described in the docs

    Args:
        old_node_name (str): The name of the old node that has been deleted
        new_node_name (str): The name of the new node that has been created

    Returns:
        bool: True if all the verification steps passed. False otherwise

    """
    if old_node_name == new_node_name:
        log.warning("Hostname didn't change")
        return False

    wait_for_nodes_status([new_node_name])
    # It can take some time until all the ocs pods are up and running
    # after the process of node replacement
    if not pod.wait_for_pods_to_be_running():
        log.warning("Not all the pods in running state")
        return False

    ct_pod = pod.get_ceph_tools_pod()
    ceph_osd_status = ct_pod.exec_ceph_cmd(ceph_cmd="ceph osd status")
    if new_node_name not in ceph_osd_status:
        log.warning("new node name not found in 'ceph osd status' output")
        return False
    if old_node_name in ceph_osd_status:
        log.warning("old node name found in 'ceph osd status' output")
        return False

    osd_pods_obj = pod.get_osd_pods()
    osd_node_names = [pod.get_pod_node(p).name for p in osd_pods_obj]
    if new_node_name not in osd_node_names:
        log.warning("the new hostname not found in osd node names")
        return False
    if old_node_name in osd_node_names:
        log.warning("the old hostname found in osd node names")
        return False

    from ocs_ci.ocs.cluster import check_ceph_osd_tree_after_node_replacement

    if not check_ceph_osd_tree_after_node_replacement():
        return False

    log.info("Verification steps from the ceph side finish successfully")
    return True


def generate_node_names_for_vsphere(count, prefix="compute-"):
    """
    Generate the node names for vsphere platform

    Args:
        count (int): Number of node names to generate
        prefix (str): Prefix for node name

    Returns:
        list: List of node names

    """
    compute_node_names = get_compute_node_names()
    log.info(f"Current node names: {compute_node_names}")
    compute_node_names.sort()
    current_compute_suffix = int(compute_node_names[-1].split("-")[-1])
    return [
        f"{prefix}{current_compute_suffix + node_count}"
        for node_count in range(1, count + 1)
    ]


def is_node_labeled(node_name, label=constants.OPERATOR_NODE_LABEL):
    """
    Check if the node is labeled with a specified label.

    Args:
        node_name (str): The node name to check if it has the specific label
        label (str): The name of the label. Default value is the OCS label.

    Returns:
        bool: True if the node is labeled with the specified label. False otherwise

    """
    node_names_with_label = machine.get_labeled_nodes(label=label)
    return node_name in node_names_with_label
