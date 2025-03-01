""" Setup for Sync Gateway functional tests """

import pytest
import os
from keywords.ClusterKeywords import ClusterKeywords
from keywords.constants import CLUSTER_CONFIGS_DIR
from keywords.exceptions import ProvisioningError, FeatureSupportedError
from keywords.SyncGateway import (sync_gateway_config_path_for_mode,
                                  validate_sync_gateway_mode, get_sync_gateway_version)
from keywords.tklogging import Logging
from keywords.utils import check_xattr_support, log_info, version_is_binary, compare_versions, clear_resources_pngs, host_for_url
from libraries.NetworkUtils import NetworkUtils
from libraries.testkit import cluster
from utilities.cluster_config_utils import persist_cluster_config_environment_prop, is_x509_auth
from utilities.cluster_config_utils import get_load_balancer_ip
from libraries.provision.clean_cluster import clear_firewall_rules
from keywords.couchbaseserver import get_server_version
from libraries.testkit import prometheus


UNSUPPORTED_1_5_0_CC = {
    "test_db_offline_tap_loss_sanity[bucket_online_offline/bucket_online_offline_default_dcp-100]": {
        "reason": "Loss of DCP not longer puts the bucket in the offline state"
    },
    "test_db_offline_tap_loss_sanity[bucket_online_offline/bucket_online_offline_default-100]": {
        "reason": "Loss of DCP not longer puts the bucket in the offline state"
    },
    "test_multiple_dbs_unique_buckets_lose_tap[bucket_online_offline/bucket_online_offline_multiple_dbs_unique_buckets-100]": {
        "reason": "Loss of DCP not longer puts the bucket in the offline state"
    },
    "test_db_online_offline_webhooks_offline_two[webhooks/webhook_offline-5-1-1-2]": {
        "reason": "Loss of DCP not longer puts the bucket in the offline state"
    }
}


def skip_if_unsupported(sync_gateway_version, mode, test_name, no_conflicts_enabled):

    # sync_gateway_version >= 1.5.0 and channel cache
    if compare_versions(sync_gateway_version, "1.5.0") >= 0 and mode == 'cc':
        if test_name in UNSUPPORTED_1_5_0_CC:
            pytest.skip(UNSUPPORTED_1_5_0_CC[test_name]["reason"])

    if compare_versions(sync_gateway_version, "1.4.0") <= 0:
        if "log_rotation" in test_name or "test_backfill" in test_name or "test_awaken_backfill" in test_name:
            pytest.skip("{} test was added for sync gateway 1.4".format(test_name))

    if sync_gateway_version < "2.0.0" and no_conflicts_enabled:
        pytest.skip("{} test cannot run with no-conflicts with sg version < 2.0.0".format(test_name))


# Add custom arguments for executing tests in this directory
def pytest_addoption(parser):

    parser.addoption("--mode",
                     action="store",
                     help="Sync Gateway mode to run the test in, 'cc' for channel cache or 'di' for distributed index")

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

    parser.addoption("--ci",
                     action="store_true",
                     help="If set, will target larger cluster (3 backing servers instead of 1, 2 accels if in di mode)")

    parser.addoption("--race",
                     action="store_true",
                     help="Enable -races for Sync Gateway build. IMPORTANT - This will only work with source builds at the moment")

    parser.addoption("--xattrs",
                     action="store_true",
                     help="Use xattrs for sync meta storage. Only works with Sync Gateway 1.5.0+ and Couchbase Server 5.0+")

    parser.addoption("--collect-logs",
                     action="store_true",
                     help="Collect logs for every test. If this flag is not set, collection will only happen for test failures.")

    parser.addoption("--server-ssl",
                     action="store_true",
                     help="If set, will enable SSL communication between server and Sync Gateway")

    parser.addoption("--cbs-platform",
                     action="store",
                     help="Couchbase Server Platform binary to install (ex. centos or windows)",
                     default="centos7")

    parser.addoption("--sg-platform",
                     action="store",
                     help="Sync Gateway Platform binary to install (ex. centos or windows)",
                     default="centos")

    parser.addoption("--sg-installer-type",
                     action="store",
                     help="Sync Gateway Installer type (ex. exe or msi)",
                     default="msi")

    parser.addoption("--sa-platform",
                     action="store",
                     help="Sync Gateway Accelerator Platform binary to install (ex. centos or windows)",
                     default="centos")

    parser.addoption("--sa-installer-type",
                     action="store",
                     help="Sync Gateway Accelerator Installer type (ex. exe or msi)",
                     default="msi")

    parser.addoption("--sg-lb",
                     action="store_true",
                     help="If set, will enable load balancer for Sync Gateway")

    parser.addoption("--sg-ce",
                     action="store_true",
                     help="If set, will install CE version of Sync Gateway")

    parser.addoption("--sequoia",
                     action="store_true",
                     help="If set, the tests will use a cluster provisioned by sequoia")

    parser.addoption("--no-conflicts",
                     action="store_true",
                     help="If set, allow_conflicts is set to false in sync-gateway config")

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

    parser.addoption("--delta-sync",
                     action="store_true",
                     help="delta-sync: Enable delta-sync for sync gateway")

    parser.addoption("--magma-storage",
                     action="store_true",
                     help="magma-storage: Enable magma storage on couchbase server")

    parser.addoption("--cbs-ce", action="store_true",
                     help="If set, community edition will get picked up , default is enterprise", default=False)

    parser.addoption("--prometheus-enable",
                     action="store",
                     help="prometheus-enable:Start prometheus metrics on SyncGateway")

    parser.addoption("--hide-product-version",
                     action="store_true",
                     help="Hides SGW product version when you hit SGW url",
                     default=False)

    parser.addoption("--skip-couchbase-provision", action="store_true",
                     help="skip the bucketcreation step")

    parser.addoption("--enable-cbs-developer-preview",
                     action="store_true",
                     help="Enabling CBS developer preview",
                     default=False)

    parser.addoption("--disable-persistent-config",
                     action="store_true",
                     help="Centralized Persistent Config")


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
    mode = request.config.getoption("--mode")
    skip_provisioning = request.config.getoption("--skip-provisioning")
    ci = request.config.getoption("--ci")
    race_enabled = request.config.getoption("--race")
    cbs_ssl = request.config.getoption("--server-ssl")
    xattrs_enabled = request.config.getoption("--xattrs")
    cbs_platform = request.config.getoption("--cbs-platform")
    sg_platform = request.config.getoption("--sg-platform")
    sg_installer_type = request.config.getoption("--sg-installer-type")
    sa_platform = request.config.getoption("--sa-platform")
    sa_installer_type = request.config.getoption("--sa-installer-type")
    sg_lb = request.config.getoption("--sg-lb")
    sg_ce = request.config.getoption("--sg-ce")
    cbs_ce = request.config.getoption("--cbs-ce")
    use_sequoia = request.config.getoption("--sequoia")
    no_conflicts_enabled = request.config.getoption("--no-conflicts")
    sg_ssl = request.config.getoption("--sg-ssl")
    use_views = request.config.getoption("--use-views")
    number_replicas = request.config.getoption("--number-replicas")
    delta_sync_enabled = request.config.getoption("--delta-sync")
    magma_storage_enabled = request.config.getoption("--magma-storage")
    prometheus_enabled = request.config.getoption("--prometheus-enable")
    hide_product_version = request.config.getoption("--hide-product-version")
    skip_couchbase_provision = request.config.getoption("--skip-couchbase-provision")
    enable_cbs_developer_preview = request.config.getoption("--enable-cbs-developer-preview")
    disable_persistent_config = request.config.getoption("--disable-persistent-config")
    trace_logs = request.config.getoption("--trace_logs")

    if xattrs_enabled and version_is_binary(sync_gateway_version):
        check_xattr_support(server_version, sync_gateway_version)

    if no_conflicts_enabled and sync_gateway_version < "2.0":
        raise FeatureSupportedError('No conflicts feature not available for sync-gateway version below 2.0, so skipping the test')

    if delta_sync_enabled and sync_gateway_version < "2.5":
        raise FeatureSupportedError('Delta sync feature not available for sync-gateway version below 2.5, so skipping the test')

    log_info("server_version: {}".format(server_version))
    log_info("sync_gateway_version: {}".format(sync_gateway_version))
    log_info("mode: {}".format(mode))
    log_info("skip_provisioning: {}".format(skip_provisioning))
    log_info("race_enabled: {}".format(race_enabled))
    log_info("xattrs_enabled: {}".format(xattrs_enabled))
    log_info("sg_platform: {}".format(sg_platform))
    log_info("sg_installer_type: {}".format(sg_installer_type))
    log_info("sa_installer_type: {}".format(sa_installer_type))
    log_info("sa_platform: {}".format(sa_platform))
    log_info("sg_lb: {}".format(sg_lb))
    log_info("sg_ce: {}".format(sg_ce))
    log_info("sg_ssl: {}".format(sg_ssl))
    log_info("no conflicts enabled {}".format(no_conflicts_enabled))
    log_info("use_views: {}".format(use_views))
    log_info("number_replicas: {}".format(number_replicas))
    log_info("delta_sync_enabled: {}".format(delta_sync_enabled))
    log_info("hide_product_version: {}".format(hide_product_version))
    log_info("enable_cbs_developer_preview: {}".format(enable_cbs_developer_preview))
    log_info("disable_persistent_config: {}".format(disable_persistent_config))
    log_info("trace_logs: {}".format(trace_logs))

    # sg-ce is invalid for di mode
    if mode == "di" and sg_ce:
        raise FeatureSupportedError("SGAccel is only available as an enterprise edition")

    # Make sure mode for sync_gateway is supported ('cc' or 'di')
    validate_sync_gateway_mode(mode)

    # use base_(lb_)cc cluster config if mode is "cc" or base_(lb_)di cluster config if mode is "di"
    if ci:
        cluster_config = "{}/ci_{}".format(CLUSTER_CONFIGS_DIR, mode)
        if sg_lb:
            cluster_config = "{}/ci_lb_{}".format(CLUSTER_CONFIGS_DIR, mode)
    else:
        cluster_config = "{}/base_{}".format(CLUSTER_CONFIGS_DIR, mode)
        if sg_lb:
            cluster_config = "{}/base_lb_{}".format(CLUSTER_CONFIGS_DIR, mode)

    log_info("Using '{}' config!".format(cluster_config))

    os.environ["CLUSTER_CONFIG"] = cluster_config

    if sg_ssl:
        log_info("Enabling SSL on sync gateway")
        persist_cluster_config_environment_prop(cluster_config, 'sync_gateway_ssl', True)
    else:
        persist_cluster_config_environment_prop(cluster_config, 'sync_gateway_ssl', False)

    # Add load balancer prop and check if load balancer IP is available
    if sg_lb:
        persist_cluster_config_environment_prop(cluster_config, 'sg_lb_enabled', True)
        log_info("Running tests with load balancer enabled: {}".format(get_load_balancer_ip(cluster_config)))
    else:
        log_info("Running tests with load balancer disabled")
        persist_cluster_config_environment_prop(cluster_config, 'sg_lb_enabled', False)

    if cbs_ssl:
        log_info("Running tests with cbs <-> sg ssl enabled")
        # Enable ssl in cluster configs
        persist_cluster_config_environment_prop(cluster_config, 'cbs_ssl_enabled', True)
    else:
        log_info("Running tests with cbs <-> sg ssl disabled")
        # Disable ssl in cluster configs
        persist_cluster_config_environment_prop(cluster_config, 'cbs_ssl_enabled', False)

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

    if xattrs_enabled:
        log_info("Running test with xattrs for sync meta storage")
        persist_cluster_config_environment_prop(cluster_config, 'xattrs_enabled', True)
    else:
        log_info("Using document storage for sync meta data")
        persist_cluster_config_environment_prop(cluster_config, 'xattrs_enabled', False)

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

    if no_conflicts_enabled:
        log_info("Running with no conflicts")
        persist_cluster_config_environment_prop(cluster_config, 'no_conflicts_enabled', True)
    else:
        log_info("Running with allow conflicts")
        persist_cluster_config_environment_prop(cluster_config, 'no_conflicts_enabled', False)

    if delta_sync_enabled:
        log_info("Running with delta sync")
        persist_cluster_config_environment_prop(cluster_config, 'delta_sync_enabled', True)
    else:
        log_info("Running without delta sync")
        persist_cluster_config_environment_prop(cluster_config, 'delta_sync_enabled', False)

    try:
        cbs_platform
    except NameError:
        log_info("cbs platform  is not provided, so by default it runs on Centos7")
        persist_cluster_config_environment_prop(cluster_config, 'cbs_platform', "centos7", False)
    else:
        log_info("Running test with cbs platform {}".format(cbs_platform))
        persist_cluster_config_environment_prop(cluster_config, 'cbs_platform', cbs_platform, False)

    try:
        sg_platform
    except NameError:
        log_info("sg platform  is not provided, so by default it runs on Centos")
        persist_cluster_config_environment_prop(cluster_config, 'sg_platform', "centos", False)
    else:
        log_info("Running test with sg platform {}".format(sg_platform))
        persist_cluster_config_environment_prop(cluster_config, 'sg_platform', sg_platform, False)

    try:
        cbs_ce
    except NameError:
        log_info("cbs ce flag  is not provided, so by default it runs on Enterprise edition")
    else:
        log_info("Running test with CBS edition {}".format(cbs_ce))
        persist_cluster_config_environment_prop(cluster_config, 'cbs_ce', cbs_ce, False)
    if magma_storage_enabled:
        log_info("Running with magma storage")
        persist_cluster_config_environment_prop(cluster_config, 'magma_storage_enabled', True, False)
    else:
        log_info("Running without magma storage")
        persist_cluster_config_environment_prop(cluster_config, 'magma_storage_enabled', False, False)

    try:
        cbs_ce
    except NameError:
        log_info("cbs ce flag  is not provided, so by default it runs on Enterprise edition")
    else:
        log_info("Running test with CBS edition {}".format(cbs_ce))
        persist_cluster_config_environment_prop(cluster_config, 'cbs_ce', cbs_ce, False)

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

    if trace_logs:
        log_info("Enabled trace logs for Sync Gateway")
        persist_cluster_config_environment_prop(cluster_config, 'trace_logs', True)
    else:
        persist_cluster_config_environment_prop(cluster_config, 'trace_logs', False)

    """if disable_persistent_config:
        sg_config = sync_gateway_config_path_for_mode("sync_gateway_default_functional_tests", mode)
    else:
        sg_config = sync_gateway_config_path_for_mode("sync_gateway_default_functional_tests", mode, cpc=True)"""
    # sg_config = sync_gateway_config_path_for_mode(sgw_config, mode)
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
                race_enabled=race_enabled,
                cbs_platform=cbs_platform,
                sg_platform=sg_platform,
                sg_installer_type=sg_installer_type,
                sa_platform=sa_platform,
                sa_installer_type=sa_installer_type,
                sg_ce=sg_ce,
                cbs_ce=cbs_ce,
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
    cluster_utils = ClusterKeywords(cluster_config)
    cluster_topology = cluster_utils.get_cluster_topology(cluster_config)

    if prometheus_enabled:
        if not prometheus.is_prometheus_installed():
            prometheus.install_prometheus()
        cluster_topology = cluster_utils.get_cluster_topology(cluster_config)
        sg_url = cluster_topology["sync_gateways"][0]["public"]
        sg_ip = host_for_url(sg_url)
        prometheus.start_prometheus(sg_ip, sg_ssl)

    yield {
        "sync_gateway_version": sync_gateway_version,
        "cluster_config": cluster_config,
        "cluster_topology": cluster_topology,
        "mode": mode,
        "xattrs_enabled": xattrs_enabled,
        "sg_lb": sg_lb,
        "no_conflicts_enabled": no_conflicts_enabled,
        "sg_platform": sg_platform,
        "ssl_enabled": cbs_ssl,
        "delta_sync_enabled": delta_sync_enabled,
        "sg_ce": sg_ce,
        "sg_config": sg_config,
        "cbs_ce": cbs_ce,
        "prometheus_enabled": prometheus_enabled,
        "disable_persistent_config": disable_persistent_config
    }

    log_info("Tearing down 'params_from_base_suite_setup' ...")

    if prometheus_enabled:
        prometheus.stop_prometheus(sg_ip, sg_ssl)

    # clean up firewall rules if any ports blocked for server ssl testing
    clear_firewall_rules(cluster_config)
    # Stop all sync_gateway and sg_accels as test finished
    c = cluster.Cluster(cluster_config)
    c.stop_sg_and_accel()

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
    sg_lb = params_from_base_suite_setup["sg_lb"]
    no_conflicts_enabled = params_from_base_suite_setup["no_conflicts_enabled"]
    cbs_ssl = params_from_base_suite_setup["ssl_enabled"]
    sync_gateway_version = params_from_base_suite_setup["sync_gateway_version"]
    sg_platform = params_from_base_suite_setup["sg_platform"]
    delta_sync_enabled = params_from_base_suite_setup["delta_sync_enabled"]
    sg_ce = params_from_base_suite_setup["sg_ce"]
    sg_config = params_from_base_suite_setup["sg_config"]
    cbs_ce = params_from_base_suite_setup["cbs_ce"]

    test_name = request.node.name
    c = cluster.Cluster(cluster_config)
    sg = c.sync_gateways[0]
    cbs_ip = c.servers[0].host

    try:
        get_sync_gateway_version(sg.ip)
    except Exception:
        try:
            get_server_version(cbs_ip, cbs_ssl=cbs_ssl)
        except Exception:
            c.reset(sg_config_path=sg_config)
        sg.restart(config=sg_config, cluster_config=cluster_config)

    if sg_lb:
        # These tests target one SG node
        skip_tests = ['resync', 'log_rotation', 'openidconnect']
        for test in skip_tests:
            if test in test_name:
                pytest.skip("Skipping online/offline tests with load balancer")
    if is_x509_auth(cluster_config) and mode == "di":
        pytest.skip("x509 certificate authentication is not supoorted in DI mode")

    # Certain test are diabled for certain modes
    # Given the run conditions, check if the test needs to be skipped
    skip_if_unsupported(
        sync_gateway_version=params_from_base_suite_setup["sync_gateway_version"],
        mode=mode,
        test_name=test_name,
        no_conflicts_enabled=no_conflicts_enabled
    )

    cluster_helper = ClusterKeywords(cluster_config)
    cluster_hosts = cluster_helper.get_cluster_topology(cluster_config=cluster_config)
    sg_url = cluster_hosts["sync_gateways"][0]["public"]
    sg_admin_url = cluster_hosts["sync_gateways"][0]["admin"]

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
        "no_conflicts_enabled": no_conflicts_enabled,
        "sync_gateway_version": sync_gateway_version,
        "sg_platform": sg_platform,
        "ssl_enabled": cbs_ssl,
        "delta_sync_enabled": delta_sync_enabled,
        "sg_ce": sg_ce,
        "cbs_ce": cbs_ce,
        "sg_url": sg_url,
        "sg_admin_url": sg_admin_url
    }

    # Code after the yield will execute when each test finishes
    log_info("Tearing down test '{}'".format(test_name))

    network_utils = NetworkUtils()
    network_utils.list_connections()

    # Verify all sync_gateways and sg_accels are reachable

    errors = c.verify_alive(mode)

    # if the test failed or a node is down, pull logs
    if collect_logs or request.node.rep_call.failed or len(errors) != 0:
        logging_helper = Logging()
        logging_helper.fetch_and_analyze_logs(cluster_config=cluster_config, test_name=test_name)
