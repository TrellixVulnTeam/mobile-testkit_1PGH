""" Setup for Sync Gateway functional tests """

import pytest
import os

from keywords.ClusterKeywords import ClusterKeywords
from keywords.constants import CLUSTER_CONFIGS_DIR
from keywords.exceptions import ProvisioningError
from keywords.SyncGateway import (sync_gateway_config_path_for_mode,
                                  validate_sync_gateway_mode)
from keywords.tklogging import Logging
from keywords.utils import check_xattr_support, log_info, version_is_binary, clear_resources_pngs
from libraries.NetworkUtils import NetworkUtils
from libraries.testkit import cluster
from utilities.cluster_config_utils import persist_cluster_config_environment_prop
from keywords.exceptions import LogScanningError
from libraries.provision.ansible_runner import AnsibleRunner


# Add custom arguments for executing tests in this directory
def pytest_addoption(parser):

    parser.addoption("--mode",
                     action="store",
                     help="Sync Gateway mode to run the test in, 'cc' for channel cache or 'di' for distributed index")

    parser.addoption("--sequoia",
                     action="store_true",
                     help="Pass this if the cluster has been provisioned via sequoia")

    parser.addoption("--skip-provisioning",
                     action="store_true",
                     help="Skip cluster provisioning at setup",
                     default=False)

    parser.addoption("--server-version",
                     action="store",
                     help="server-version: Couchbase Server version to install (ex. 4.5.0 or 4.5.0-2601)")

    parser.addoption("--sync-gateway-version",
                     action="store",
                     help="sync-gateway-version: Sync Gateway version to install (ex. 1.3.1-16 or 590c1c31c7e83503eff304d8c0789bdd268d6291)")

    parser.addoption("--xattrs",
                     action="store_true",
                     help="Use xattrs for sync meta storage. Only works with Sync Gateway 2.0+ and Couchbase Server 5.0+")

    parser.addoption("--collect-logs",
                     action="store_true",
                     help="Collect logs for every test. If this flag is not set, collection will only happen for test failures.")

    parser.addoption("--server-ssl",
                     action="store_true",
                     help="If set, will enable SSL communication between server and Sync Gateway")

    parser.addoption("--server-seed-docs",
                     action="store",
                     help="server-seed-docs: Number of docs to seed the Couchbase server with")

    parser.addoption("--max-docs",
                     action="store",
                     help="max-doc-size: Max number of docs to run the test with")

    parser.addoption("--num-users",
                     action="store",
                     help="num-users: Number of users to run the simulation with")

    parser.addoption("--create-batch-size",
                     action="store",
                     help="create-batch-size: Number of docs to add in bulk on each POST creation")

    parser.addoption("--create-delay",
                     action="store",
                     help="create-delay: Delay between each bulk POST operation")

    parser.addoption("--update-runtime-sec",
                     action="store",
                     help="--update-runtime-sec: Number of seconds to continue updates for")

    parser.addoption("--update-batch-size",
                     action="store",
                     help="update-batch-size: Number of docs to add in bulk on each POST update")

    parser.addoption("--update-docs-percentage",
                     action="store",
                     help="update-docs-percentage: Percentage of user docs to update on each batch")

    parser.addoption("--update-delay",
                     action="store",
                     help="update-delay: Delay between each bulk POST operation for updates")

    parser.addoption("--changes-delay",
                     action="store",
                     help="changes-delay: Delay between each _changes request")

    parser.addoption("--changes-limit",
                     action="store",
                     help="changes-limit: Amount of docs to return per changes request")

    parser.addoption("--sg-ssl",
                     action="store_true",
                     help="If set, will enable SSL communication between Sync Gateway and CBL")

    parser.addoption("--use-views",
                     action="store_true",
                     help="If set, uses views instead of GSI - SG 2.1 and above only")

    parser.addoption("--number-replicas",
                     action="store",
                     help="Number of replicas for the indexer node - SG 2.1 and above only",
                     default=0)

    parser.addoption("--hide-product-version",
                     action="store_true",
                     help="Hides SGW product version when you hit SGW url",
                     default=False)

    parser.addoption("--skip-couchbase-provision",
                     action="store_true",
                     help="skip the couchbase provision step")

    parser.addoption("--enable-cbs-developer-preview",
                     action="store_true",
                     help="Enabling CBS developer preview",
                     default=False)

    parser.addoption("--disable-persistent-config",
                     action="store_true",
                     help="Disable Centralized Persistent Config")

    parser.addoption("--enable-server-tls-skip-verify",
                     action="store_true",
                     help="Enable Server tls skip verify config")

    parser.addoption("--disable-tls-server",
                     action="store_true",
                     help="Disable tls server")

    parser.addoption("--disable-admin-auth",
                     action="store_true",
                     help="Disable Admin auth")


# This will be called once for the at the beggining of the execution in the 'tests/' directory
# and will be torn down, (code after the yeild) when all the test session has completed.
# IMPORTANT: Tests in 'tests/' should be executed in their own test run and should not be
# run in the same test run with 'topology_specific_tests/'. Doing so will make have unintended
# side effects due to the session scope


@pytest.fixture(scope="session")
def params_from_base_suite_setup(request):
    log_info("Setting up 'params_from_base_suite_setup' ...")

    # pytest command line parameters
    server_version = request.config.getoption("--server-version")
    sync_gateway_version = request.config.getoption("--sync-gateway-version")
    disable_tls_server = request.config.getoption("--disable-tls-server")
    mode = request.config.getoption("--mode")
    use_sequoia = request.config.getoption("--sequoia")
    skip_provisioning = request.config.getoption("--skip-provisioning")
    cbs_ssl = request.config.getoption("--server-ssl")
    xattrs_enabled = request.config.getoption("--xattrs")
    server_seed_docs = request.config.getoption("--server-seed-docs")
    max_docs = request.config.getoption("--max-docs")
    num_users = request.config.getoption("--num-users")
    create_batch_size = request.config.getoption("--create-batch-size")
    create_delay = request.config.getoption("--create-delay")
    update_runtime_sec = request.config.getoption("--update-runtime-sec")
    update_batch_size = request.config.getoption("--update-batch-size")
    update_docs_percentage = request.config.getoption("--update-docs-percentage")
    update_delay = request.config.getoption("--update-delay")
    changes_delay = request.config.getoption("--changes-delay")
    changes_limit = request.config.getoption("--changes-limit")
    sg_ssl = request.config.getoption("--sg-ssl")
    use_views = request.config.getoption("--use-views")
    number_replicas = request.config.getoption("--number-replicas")
    hide_product_version = request.config.getoption("--hide-product-version")
    skip_couchbase_provision = request.config.getoption("--skip-couchbase-provision")
    enable_cbs_developer_preview = request.config.getoption("--enable-cbs-developer-preview")
    disable_persistent_config = request.config.getoption("--disable-persistent-config")
    enable_server_tls_skip_verify = request.config.getoption("--enable-server-tls-skip-verify")
    disable_tls_server = request.config.getoption("--disable-tls-server")

    disable_admin_auth = request.config.getoption("--disable-admin-auth")
    trace_logs = request.config.getoption("--trace_logs")

    if xattrs_enabled and version_is_binary(sync_gateway_version):
        check_xattr_support(server_version, sync_gateway_version)

    log_info("server_version: {}".format(server_version))
    log_info("sync_gateway_version: {}".format(sync_gateway_version))
    log_info("mode: {}".format(mode))
    log_info("skip_provisioning: {}".format(skip_provisioning))
    log_info("cbs_ssl: {}".format(cbs_ssl))
    log_info("xattrs_enabled: {}".format(xattrs_enabled))
    log_info("sg_ssl: {}".format(sg_ssl))
    log_info("use_views: {}".format(use_views))
    log_info("number_replicas: {}".format(number_replicas))
    log_info("hide_product_version: {}".format(hide_product_version))
    log_info("enable_cbs_developer_preview: {}".format(enable_cbs_developer_preview))
    log_info("disable_persistent_config: {}".format(disable_persistent_config))
    log_info("trace_logs: {}".format(trace_logs))

    # Make sure mode for sync_gateway is supported ('cc' or 'di')
    validate_sync_gateway_mode(mode)

    # use ci_lb_cc cluster config if mode is "cc" or ci_lb_di cluster config if more is "di"
    log_info("Using 'ci_lb_{}' config!".format(mode))
    cluster_config = "{}/ci_lb_{}".format(CLUSTER_CONFIGS_DIR, mode)
    cluster_utils = ClusterKeywords(cluster_config)

    try:
        server_version
    except NameError:
        log_info("Server version is not provided")
        persist_cluster_config_environment_prop(cluster_config, 'server_version', "")
    else:
        log_info("Running test with server version {}".format(server_version))
        persist_cluster_config_environment_prop(cluster_config, 'server_version', server_version)

    try:
        sync_gateway_version
    except NameError:
        log_info("Sync gateway version is not provided")
        persist_cluster_config_environment_prop(cluster_config, 'sync_gateway_version', "")
    else:
        log_info("Running test with sync_gateway version {}".format(sync_gateway_version))
        persist_cluster_config_environment_prop(cluster_config, 'sync_gateway_version', sync_gateway_version)

    # Only works with load balancer configs
    persist_cluster_config_environment_prop(cluster_config, 'sg_lb_enabled', True)

    if sg_ssl:
        log_info("Enabling SSL on sync gateway")
        persist_cluster_config_environment_prop(cluster_config, 'sync_gateway_ssl', True)
    else:
        persist_cluster_config_environment_prop(cluster_config, 'sync_gateway_ssl', False)

    if use_views:
        log_info("Running SG tests using views")
        # Enable sg views in cluster configs
        persist_cluster_config_environment_prop(cluster_config, 'sg_use_views', True)
    else:
        log_info("Running tests with cbs <-> sg ssl disabled")
        # Disable sg views in cluster configs
        persist_cluster_config_environment_prop(cluster_config, 'sg_use_views', False)

    # Write the number of replicas to cluster config
    persist_cluster_config_environment_prop(cluster_config, 'number_replicas', number_replicas)

    if cbs_ssl:
        log_info("Running tests with cbs <-> sg ssl enabled")
        # Enable ssl in cluster configs
        persist_cluster_config_environment_prop(cluster_config, 'cbs_ssl_enabled', True)
    else:
        log_info("Running tests with cbs <-> sg ssl disabled")
        # Disable ssl in cluster configs
        persist_cluster_config_environment_prop(cluster_config, 'cbs_ssl_enabled', False)

    if xattrs_enabled:
        log_info("Running test with xattrs for sync meta storage")
        persist_cluster_config_environment_prop(cluster_config, 'xattrs_enabled', True)
    else:
        log_info("Using document storage for sync meta data")
        persist_cluster_config_environment_prop(cluster_config, 'xattrs_enabled', False)

    if hide_product_version:
        log_info("Suppress the SGW product Version")
        persist_cluster_config_environment_prop(cluster_config, 'hide_product_version', True)
    else:
        log_info("Running without suppress SGW product Version")
        persist_cluster_config_environment_prop(cluster_config, 'hide_product_version', False)

    if enable_cbs_developer_preview:
        log_info("Enable CBS developer preview")
        persist_cluster_config_environment_prop(cluster_config, 'cbs_developer_preview', True)
    else:
        log_info("Running without CBS developer preview")
        persist_cluster_config_environment_prop(cluster_config, 'cbs_developer_preview', False)

    if disable_persistent_config:
        log_info(" disable persistent config")
        persist_cluster_config_environment_prop(cluster_config, 'disable_persistent_config', True)
    else:
        log_info("Running without Centralized Persistent Config")
        persist_cluster_config_environment_prop(cluster_config, 'disable_persistent_config', False)

    if enable_server_tls_skip_verify:
        log_info("Enable server tls skip verify flag")
        persist_cluster_config_environment_prop(cluster_config, 'server_tls_skip_verify', True)
    else:
        log_info("Running without server_tls_skip_verify Config")
        persist_cluster_config_environment_prop(cluster_config, 'server_tls_skip_verify', False)

    if disable_tls_server:
        log_info("Disable tls server flag")
        persist_cluster_config_environment_prop(cluster_config, 'disable_tls_server', True)
    else:
        log_info("Enable tls server flag")
        persist_cluster_config_environment_prop(cluster_config, 'disable_tls_server', False)

    if disable_admin_auth:
        log_info("Disabled Admin Auth")
        persist_cluster_config_environment_prop(cluster_config, 'disable_admin_auth', True)
    else:
        log_info("Enabled Admin Auth")
        persist_cluster_config_environment_prop(cluster_config, 'disable_admin_auth', False)

    if trace_logs:
        log_info("Enabled trace logs for Sync Gateway")
        persist_cluster_config_environment_prop(cluster_config, 'trace_logs', True)
    else:
        persist_cluster_config_environment_prop(cluster_config, 'trace_logs', False)

    sg_config = sync_gateway_config_path_for_mode("sync_gateway_default_functional_tests", mode)

    # Skip provisioning if user specifies '--skip-provisoning' or '--sequoia'
    should_provision = True
    if skip_provisioning or use_sequoia:
        should_provision = False

    cluster_utils = ClusterKeywords(cluster_config)
    if should_provision:
        try:
            cluster_utils.provision_cluster(
                cluster_config=cluster_config,
                server_version=server_version,
                sync_gateway_version=sync_gateway_version,
                sync_gateway_config=sg_config,
                skip_couchbase_provision=skip_couchbase_provision
            )
        except ProvisioningError:
            logging_helper = Logging()
            logging_helper.fetch_and_analyze_logs(cluster_config=cluster_config, test_name=request.node.name)
            raise

    # Hit this intalled running services to verify the correct versions are installed
    cluster_utils.verify_cluster_versions(
        cluster_config,
        expected_server_version=server_version,
        expected_sync_gateway_version=sync_gateway_version
    )

    # Load topology as a dictionary
    cluster_topology = cluster_utils.get_cluster_topology(cluster_config)

    yield {
        "cluster_config": cluster_config,
        "cluster_topology": cluster_topology,
        "mode": mode,
        "xattrs_enabled": xattrs_enabled,
        "server_seed_docs": server_seed_docs,
        "max_docs": max_docs,
        "num_users": num_users,
        "create_batch_size": create_batch_size,
        "create_delay": create_delay,
        "update_runtime_sec": update_runtime_sec,
        "update_batch_size": update_batch_size,
        "update_docs_percentage": update_docs_percentage,
        "update_delay": update_delay,
        "changes_delay": changes_delay,
        "changes_limit": changes_limit,
        "trace_logs": trace_logs
    }

    log_info("Tearing down 'params_from_base_suite_setup' ...")
    # Delete png files under resources/data
    clear_resources_pngs()


# This is called before each test and will yield the dictionary to each test that references the method
# as a parameter to the test method
@pytest.fixture(scope="function")
def params_from_base_test_setup(request, params_from_base_suite_setup):
    # Code before the yeild will execute before each test starts

    # pytest command line parameters
    collect_logs = request.config.getoption("--collect-logs")

    cluster_config = params_from_base_suite_setup["cluster_config"]
    cluster_topology = params_from_base_suite_setup["cluster_topology"]
    mode = params_from_base_suite_setup["mode"]
    xattrs_enabled = params_from_base_suite_setup["xattrs_enabled"]
    trace_logs = params_from_base_suite_setup["trace_logs"]

    test_name = request.node.name
    log_info("Running test '{}'".format(test_name))
    log_info("cluster_config: {}".format(cluster_config))
    log_info("cluster_topology: {}".format(cluster_topology))
    log_info("mode: {}".format(mode))
    log_info("xattrs_enabled: {}".format(xattrs_enabled))

    # This dictionary is passed to each test
    yield {
        "cluster_config": cluster_config,
        "cluster_topology": cluster_topology,
        "mode": mode,
        "xattrs_enabled": xattrs_enabled,
        "server_seed_docs": params_from_base_suite_setup["server_seed_docs"],
        "max_docs": params_from_base_suite_setup["max_docs"],
        "num_users": params_from_base_suite_setup["num_users"],
        "create_batch_size": params_from_base_suite_setup["create_batch_size"],
        "create_delay": params_from_base_suite_setup["create_delay"],
        "update_runtime_sec": params_from_base_suite_setup["update_runtime_sec"],
        "update_batch_size": params_from_base_suite_setup["update_batch_size"],
        "update_docs_percentage": params_from_base_suite_setup["update_docs_percentage"],
        "update_delay": params_from_base_suite_setup["update_delay"],
        "changes_delay": params_from_base_suite_setup["changes_delay"],
        "changes_limit": params_from_base_suite_setup["changes_limit"],
        "trace_logs": trace_logs
    }

    # Code after the yield will execute when each test finishes
    log_info("Tearing down test '{}'".format(test_name))

    network_utils = NetworkUtils()
    network_utils.list_connections()

    # Verify all sync_gateways and sg_accels are reachable
    c = cluster.Cluster(cluster_config)
    errors = c.verify_alive(mode)

    # if the test failed or a node is down, pull logs
    logging_helper = Logging()
    if collect_logs or request.node.rep_call.failed or len(errors) != 0:
        logging_helper.fetch_and_analyze_logs(cluster_config=cluster_config, test_name=test_name)

    # Scan logs
    # SG logs for panic, data race
    # System logs for OOM
    ansible_runner = AnsibleRunner(cluster_config)
    script_name = "{}/utilities/check_logs.sh".format(os.getcwd())
    status = ansible_runner.run_ansible_playbook(
        "check-logs.yml",
        extra_vars={
            "script_name": script_name
        }
    )

    if status != 0:
        logging_helper.fetch_and_analyze_logs(cluster_config=cluster_config, test_name=test_name)
        raise LogScanningError("Errors found in the logs")
