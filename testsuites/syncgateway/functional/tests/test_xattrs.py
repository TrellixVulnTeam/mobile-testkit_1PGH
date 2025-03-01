import random
import time
import json
import requests

import pytest
import concurrent.futures
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import ThreadPoolExecutor

from couchbase.exceptions import CouchbaseException, DocumentNotFoundException
from requests.exceptions import HTTPError
from keywords.exceptions import ChangesError

from keywords import attachment, document
from keywords.constants import DATA_DIR
from keywords.MobileRestClient import MobileRestClient
from keywords.SyncGateway import sync_gateway_config_path_for_mode
from keywords.SyncGateway import SyncGateway
from keywords.userinfo import UserInfo
from keywords.utils import host_for_url, log_info
from libraries.testkit.cluster import Cluster
from keywords.ChangesTracker import ChangesTracker
from utilities.cluster_config_utils import get_sg_use_views, get_sg_version, persist_cluster_config_environment_prop, copy_to_temp_conf, get_cluster
from libraries.testkit.syncgateway import get_buckets_from_sync_gateway_config
from keywords.constants import RBAC_FULL_ADMIN


# Since sdk is quicker to update docs we need to have it sleep longer
# between ops to avoid ops heavily weighted to SDK. These gives us more balanced
# concurrency for each client.
SG_OP_SLEEP = 0.001
SDK_OP_SLEEP = 0.05


@pytest.mark.syncgateway
@pytest.mark.xattrs
@pytest.mark.session
@pytest.mark.oscertify
@pytest.mark.parametrize('sg_conf_name', [
    'xattrs/old_doc'
])
def test_olddoc_nil(params_from_base_test_setup, sg_conf_name):
    """ Regression test for - https://github.com/couchbase/sync_gateway/issues/2565

    Using the custom sync function:
        function(doc, oldDoc) {
            if (oldDoc != null) {
                throw({forbidden: "Old doc should be null!"})
            } else {
                console.log("oldDoc is null");
                console.log(doc.channels);
                channel(doc.channels);
            }
        }

    1. Create user with channel 'ABC' (user1)
    2. Create user with channel 'CBS' (user2)
    3. Write doc with channel 'ABC'
    4. Verify that user1 can see the doc and user2 cannot
    5. SDK updates the doc channel to 'CBS'
    6. This should result in a new rev but with oldDoc == nil (due to SDK mutation)
    7. Assert that user2 can see the doc and user1 cannot
    """

    # bucket_name = 'data-bucket'
    sg_db = 'db'
    num_docs = 1000

    cluster_conf = params_from_base_test_setup['cluster_config']
    cluster_topology = params_from_base_test_setup['cluster_topology']
    mode = params_from_base_test_setup['mode']
    xattrs_enabled = params_from_base_test_setup['xattrs_enabled']
    delta_sync_enabled = params_from_base_test_setup['delta_sync_enabled']
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    # This test should only run when using xattr meta storage
    if not xattrs_enabled or delta_sync_enabled:
        pytest.skip('XATTR tests require --xattrs flag or test has enable delta sync')

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)
    sg_admin_url = cluster_topology['sync_gateways'][0]['admin']
    sg_url = cluster_topology['sync_gateways'][0]['public']
    cbs_url = cluster_topology['couchbase_servers'][0]
    ssl_enabled = params_from_base_test_setup["ssl_enabled"]
    buckets = get_buckets_from_sync_gateway_config(sg_conf, cluster_conf)
    bucket_name = buckets[0]
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None

    log_info('sg_conf: {}'.format(sg_conf))
    log_info('sg_admin_url: {}'.format(sg_admin_url))
    log_info('sg_url: {}'.format(sg_url))
    log_info('cbs_url: {}'.format(cbs_url))

    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_config_path=sg_conf)

    if sync_gateway_version >= "2.5.0":
        sg_client = MobileRestClient()
        expvars = sg_client.get_expvars(sg_admin_url, auth=auth)
        error_count = expvars["syncgateway"]["global"]["resource_utilization"]["error_count"]

    # Create clients
    sg_client = MobileRestClient()
    cbs_ip = host_for_url(cbs_url)
    if ssl_enabled and cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify&ipv6=allow".format(cbs_ip)
    elif ssl_enabled and not cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify".format(cbs_ip)
    elif not ssl_enabled and cluster.ipv6:
        connection_url = "couchbase://{}?ipv6=allow".format(cbs_ip)
    else:
        connection_url = 'couchbase://{}'.format(cbs_ip)
    sdk_client = get_cluster(connection_url, bucket_name)
    # Create user / session
    user_one_info = UserInfo(name='user1', password='pass', channels=['ABC'], roles=[])
    user_two_info = UserInfo(name='user2', password='pass', channels=['CBS'], roles=[])

    for user in [user_one_info, user_two_info]:
        sg_client.create_user(
            url=sg_admin_url,
            db=sg_db,
            name=user.name,
            password=user.password,
            channels=user.channels,
            auth=auth
        )

    user_one_auth = sg_client.create_session(
        url=sg_admin_url,
        db=sg_db,
        name=user_one_info.name,
        auth=auth
    )

    user_two_auth = sg_client.create_session(
        url=sg_admin_url,
        db=sg_db,
        name=user_two_info.name,
        auth=auth
    )

    abc_docs = document.create_docs(doc_id_prefix="abc_docs", number=num_docs, channels=user_one_info.channels)
    abc_doc_ids = [doc['_id'] for doc in abc_docs]

    user_one_docs = sg_client.add_bulk_docs(url=sg_url, db=sg_db, docs=abc_docs, auth=user_one_auth)
    assert len(user_one_docs) == num_docs

    # Issue bulk_get from user_one and assert that user_one, can see all of the docs
    user_one_bulk_get_docs, errors = sg_client.get_bulk_docs(url=sg_url, db=sg_db, doc_ids=abc_doc_ids, auth=user_one_auth)
    assert len(user_one_bulk_get_docs) == num_docs
    assert len(errors) == 0

    # Issue bulk_get from user_two and assert that user_two cannot see any of the docs
    user_two_bulk_get_docs, errors = sg_client.get_bulk_docs(url=sg_url, db=sg_db, doc_ids=abc_doc_ids, auth=user_two_auth, validate=False)
    assert len(user_two_bulk_get_docs) == 0
    assert len(errors) == num_docs

    # Update the channels of each doc to 'NBC'
    for abc_doc_id in abc_doc_ids:
        doc = sdk_client.get(abc_doc_id)
        doc_body = doc.content
        doc_body['channels'] = user_two_info.channels
        sdk_client.upsert(abc_doc_id, doc_body)

    # Issue bulk_get from user_one and assert that user_one can't see any docs
    user_one_bulk_get_docs, errors = sg_client.get_bulk_docs(url=sg_url, db=sg_db, doc_ids=abc_doc_ids, auth=user_one_auth, validate=False)
    assert len(user_one_bulk_get_docs) == 0
    assert len(errors) == num_docs

    # Issue bulk_get from user_two and assert that user_two can see all docs
    user_two_bulk_get_docs, errors = sg_client.get_bulk_docs(url=sg_url, db=sg_db, doc_ids=abc_doc_ids, auth=user_two_auth)
    assert len(user_two_bulk_get_docs) == num_docs
    assert len(errors) == 0

    if sync_gateway_version >= "2.5.0":
        sg_client = MobileRestClient()
        expvars = sg_client.get_expvars(sg_admin_url, auth=auth)
        assert error_count < expvars["syncgateway"]["global"]["resource_utilization"]["error_count"], "error_count did not increment"


@pytest.mark.syncgateway
@pytest.mark.xattrs
@pytest.mark.session
@pytest.mark.parametrize('sg_conf_name, number_users, number_docs_per_user, number_of_updates_per_user', [
    ('xattrs/no_import', 1, 1, 10),
    pytest.param('xattrs/no_import', 100, 10, 10, marks=pytest.mark.oscertify),
    ('xattrs/no_import', 10, 1000, 10)
    # ('xattrs/no_import', 100, 1000, 2)
])
def test_on_demand_doc_processing(params_from_base_test_setup, sg_conf_name, number_users, number_docs_per_user, number_of_updates_per_user):
    """
    1. Start Sync Gateway with autoimport disabled, this will force on-demand processing
    1. Create 100 users (user_0, user_1, ...) on Sync Gateway each with their own channel (user_0_chan, user_1_chan, ...)
    1. Load 'number_doc_per_user' with channels specified to each user from SDK
    1. Make sure the docs are imported via GET /db/doc and POST _bulk_get
    """

    cluster_conf = params_from_base_test_setup['cluster_config']
    cluster_topology = params_from_base_test_setup['cluster_topology']
    mode = params_from_base_test_setup['mode']
    xattrs_enabled = params_from_base_test_setup['xattrs_enabled']
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    cbs_url = cluster_topology['couchbase_servers'][0]
    sg_admin_url = cluster_topology['sync_gateways'][0]['admin']
    sg_url = cluster_topology['sync_gateways'][0]['public']
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None

    # bucket_name = 'data-bucket'
    sg_db = 'db'
    cbs_host = host_for_url(cbs_url)

    # This test should only run when using xattr meta storage
    if not xattrs_enabled:
        pytest.skip('XATTR tests require --xattrs flag')

    # This test should only run when mode is CC
    if mode == "di":
        pytest.skip('This test does not run in DI mode')

    # Reset cluster
    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)
    buckets = get_buckets_from_sync_gateway_config(sg_conf, cluster_conf)
    bucket_name = buckets[0]
    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_config_path=sg_conf)

    log_info("Number of users: {}".format(number_users))
    log_info("Number of docs per user: {}".format(number_docs_per_user))
    log_info("Number of update per user: {}".format(number_of_updates_per_user))

    # Initialize clients
    sg_client = MobileRestClient()
    # TODO : Add support for ssl enabled once ssl enabled support is merged to master
    if cluster.ipv6:
        url = "couchbase://{}?ipv6=allow".format(cbs_host)
    else:
        url = "couchbase://{}".format(cbs_host)
    sdk_client = get_cluster(url, bucket_name)

    # Create Sync Gateway user
    auth_dict = {}
    docs_to_add = {}
    user_names = ['user_{}'.format(i) for i in range(number_users)]

    def update_props():
        return {
            'updates': 0
        }

    for user_name in user_names:
        user_channels = ['{}_chan'.format(user_name)]
        sg_client.create_user(url=sg_admin_url, db=sg_db, name=user_name, password='pass', channels=user_channels, auth=auth)
        auth_dict[user_name] = sg_client.create_session(url=sg_admin_url, db=sg_db, name=user_name, auth=auth)
        docs = document.create_docs('{}_doc'.format(user_name), number=number_docs_per_user, channels=user_channels, prop_generator=update_props, non_sgw=True)
        for doc in docs:
            docs_to_add[doc['id']] = doc

    assert len(docs_to_add) == number_users * number_docs_per_user

    # Add the docs via
    log_info('Adding docs via SDK ...')
    for k, v in docs_to_add.items():
        sdk_client.upsert(k, v)

    assert len(docs_to_add) == number_users * number_docs_per_user

    # issue _bulk_get
    with ProcessPoolExecutor() as ppe:

        user_gets = {}
        user_writes = {}

        # Start issuing bulk_gets concurrently
        for user_name in user_names:
            user_doc_ids = ['{}_doc_{}'.format(user_name, i) for i in range(number_docs_per_user)]
            future = ppe.submit(
                sg_client.get_bulk_docs,
                url=sg_url,
                db=sg_db,
                doc_ids=user_doc_ids,
                auth=auth_dict[user_name]
            )
            user_gets[future] = user_name

        # Wait for all futures complete
        for future in concurrent.futures.as_completed(user_gets):
            user = user_gets[future]
            docs, errors = future.result()
            assert len(docs) == number_docs_per_user
            assert len(errors) == 0
            log_info('Docs found for user ({}): {}'.format(user, len(docs)))

        # Start concurrent updates from Sync Gateway side
        for user_name in user_names:
            log_info('Starting concurrent updates!')
            user_doc_ids = ['{}_doc_{}'.format(user_name, i) for i in range(number_docs_per_user)]
            assert len(user_doc_ids) == number_docs_per_user
            # Start updating from sync gateway for completed user
            write_future = ppe.submit(
                update_sg_docs,
                client=sg_client,
                url=sg_url,
                db=sg_db,
                docs_to_update=user_doc_ids,
                prop_to_update='updates',
                number_updates=number_of_updates_per_user,
                auth=auth_dict[user_name]
            )
            user_writes[write_future] = user

        for future in concurrent.futures.as_completed(user_writes):
            user = user_writes[future]
            # This will bubble up exceptions
            future.result()
            log_info('Update complete for user: {}'.format(user))
    for user_name in user_names:
        all_docs_total = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=auth_dict[user_name])
        all_docs_per_user = all_docs_total["rows"]
        assert len(all_docs_per_user) == number_docs_per_user, "All documents are not returned for the user {}".format(auth_dict[user_name])


@pytest.mark.sanity
@pytest.mark.syncgateway
@pytest.mark.xattrs
@pytest.mark.session
@pytest.mark.oscertify
@pytest.mark.parametrize('sg_conf_name, x509_cert_auth', [
    ('xattrs/no_import', False)
])
def test_on_demand_import_of_external_updates(params_from_base_test_setup, sg_conf_name, x509_cert_auth):
    """
    Scenario: On demand processing of external updates

    - Start sg with XATTRs, but not import
    - Create doc via SG, store rev (#1)
    - Update doc via SDK
    - Update doc via SG, using (#1), should fail with conflict
    """

    # bucket_name = 'data-bucket'
    sg_db = 'db'

    cluster_conf = params_from_base_test_setup['cluster_config']
    cluster_topology = params_from_base_test_setup['cluster_topology']
    mode = params_from_base_test_setup['mode']
    xattrs_enabled = params_from_base_test_setup['xattrs_enabled']
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    # This test should only run when using xattr meta storage
    if not xattrs_enabled:
        pytest.skip('XATTR tests require --xattrs flag')

    if mode == "di":
        pytest.skip('This test does not run in DI mode')

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)
    sg_admin_url = cluster_topology['sync_gateways'][0]['admin']
    sg_url = cluster_topology['sync_gateways'][0]['public']
    cbs_url = cluster_topology['couchbase_servers'][0]
    ssl_enabled = params_from_base_test_setup["ssl_enabled"]
    buckets = get_buckets_from_sync_gateway_config(sg_conf, cluster_conf)
    bucket_name = buckets[0]
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None

    log_info('sg_conf: {}'.format(sg_conf))
    log_info('sg_admin_url: {}'.format(sg_admin_url))
    log_info('sg_url: {}'.format(sg_url))
    log_info('cbs_url: {}'.format(cbs_url))
    disable_tls_server = params_from_base_test_setup["disable_tls_server"]
    if x509_cert_auth and disable_tls_server:
        pytest.skip("x509 test cannot run tls server disabled")
    if x509_cert_auth:
        temp_cluster_config = copy_to_temp_conf(cluster_conf, mode)
        persist_cluster_config_environment_prop(temp_cluster_config, 'x509_certs', True)
        persist_cluster_config_environment_prop(temp_cluster_config, 'server_tls_skip_verify', False)
        cluster_conf = temp_cluster_config
    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_config_path=sg_conf)

    # Create clients
    sg_client = MobileRestClient()
    cbs_ip = host_for_url(cbs_url)
    if ssl_enabled and cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify&ipv6=allow".format(cbs_ip)
    elif ssl_enabled and not cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify".format(cbs_ip)
    elif not ssl_enabled and cluster.ipv6:
        connection_url = "couchbase://{}?ipv6=allow".format(cbs_ip)
    else:
        connection_url = 'couchbase://{}'.format(cbs_ip)
    sdk_client = get_cluster(connection_url, bucket_name)
    # Create user / session
    seth_user_info = UserInfo(name='seth', password='pass', channels=['NASA'], roles=[])
    sg_client.create_user(
        url=sg_admin_url,
        db=sg_db,
        name=seth_user_info.name,
        password=seth_user_info.password,
        channels=seth_user_info.channels,
        auth=auth
    )

    seth_auth = sg_client.create_session(
        url=sg_admin_url,
        db=sg_db,
        name=seth_user_info.name,
        auth=auth
    )

    doc_id = 'test_doc'

    doc_body = document.create_doc(doc_id, channels=seth_user_info.channels)
    doc = sg_client.add_doc(url=sg_url, db=sg_db, doc=doc_body, auth=seth_auth)
    doc_rev_one = doc['rev']

    log_info('Created doc: {} via Sync Gateway'.format(doc))

    # Update the document via SDK
    doc_to_update = sdk_client.get(doc_id)
    doc_body = doc_to_update.content
    doc_body['updated_via_sdk'] = True
    updated_doc = sdk_client.upsert(doc_id, doc_body)
    log_info('Updated doc: {} via SDK'.format(updated_doc))

    # Try to create a revision of generation 1 from Sync Gateway.
    # If on demand importing is working as designed, it should go to the
    # bucket and see that there has been an external update and import it.
    # Sync Gateway should then get a 409 conflict when trying to update the doc
    with pytest.raises(HTTPError) as he:
        sg_client.put_doc(url=sg_url, db=sg_db, doc_id=doc_id, rev=doc_rev_one, doc_body=doc_body, auth=seth_auth)
    log_info(he.value)
    res_message = str(he.value)
    assert res_message.startswith('409')

    # Following update_doc method will get the doc with on demand processing and update the doc based on rev got from
    # get doc
    sg_updated_doc = sg_client.update_doc(url=sg_url, db=sg_db, doc_id=doc_id, auth=seth_auth)
    sg_updated_rev = sg_updated_doc["rev"]
    assert sg_updated_rev.startswith("3-")


@pytest.mark.syncgateway
@pytest.mark.xattrs
@pytest.mark.session
@pytest.mark.parametrize('sg_conf_name, x509_cert_auth', [
    pytest.param('sync_gateway_default_functional_tests', True, marks=[pytest.mark.sanity, pytest.mark.oscertify]),
    ('sync_gateway_default_functional_tests_no_port', False),
    ("sync_gateway_default_functional_tests_couchbase_protocol_withport_11210", False)
])
def test_offline_processing_of_external_updates(params_from_base_test_setup, sg_conf_name, x509_cert_auth):
    """
    Scenario:
    1. Start SG, write some docs
    2. Stop SG
    3. Update the same docs via SDK (to ensure the same vbucket is getting updated)
    4. Write some new docs for SDK (just for additional testing)
    5. Restart SG, validate that all writes from 3 and 4 have been imported (w/ correct revisions)
    """

    num_docs_per_client = 1000
    # bucket_name = 'data-bucket'
    sg_db = 'db'

    cluster_conf = params_from_base_test_setup['cluster_config']
    cluster_topology = params_from_base_test_setup['cluster_topology']
    mode = params_from_base_test_setup['mode']
    xattrs_enabled = params_from_base_test_setup['xattrs_enabled']
    ssl_enabled = params_from_base_test_setup["ssl_enabled"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    # Skip the test if ssl disabled as it cannot run without port using http protocol
    if ("sync_gateway_default_functional_tests_no_port" in sg_conf_name) and get_sg_version(cluster_conf) < "1.5.0":
        pytest.skip('couchbase/couchbases ports do not support for versions below 1.5')
    if "sync_gateway_default_functional_tests_no_port" in sg_conf_name and not ssl_enabled:
        pytest.skip('ssl disabled so cannot run without port')

    # Skip the test if ssl enabled as it cannot run using couchbase protocol
    # TODO : https://github.com/couchbaselabs/sync-gateway-accel/issues/227
    # Remove DI condiiton once above bug is fixed
    if "sync_gateway_default_functional_tests_couchbase_protocol_withport_11210" in sg_conf_name and (ssl_enabled or mode.lower() == "di"):
        pytest.skip('ssl enabled so cannot run with couchbase protocol')

    # This test should only run when using xattr meta storage
    if not xattrs_enabled:
        pytest.skip('XATTR tests require --xattrs flag')

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)
    sg_admin_url = cluster_topology['sync_gateways'][0]['admin']
    sg_url = cluster_topology['sync_gateways'][0]['public']
    cbs_url = cluster_topology['couchbase_servers'][0]
    cbs_ce_version = params_from_base_test_setup["cbs_ce"]
    buckets = get_buckets_from_sync_gateway_config(sg_conf, cluster_conf)
    bucket_name = buckets[0]
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None

    log_info('sg_conf: {}'.format(sg_conf))
    log_info('sg_admin_url: {}'.format(sg_admin_url))
    log_info('sg_url: {}'.format(sg_url))
    log_info('cbs_url: {}'.format(cbs_url))
    disable_tls_server = params_from_base_test_setup["disable_tls_server"]
    if x509_cert_auth and disable_tls_server:
        pytest.skip("x509 test cannot run tls server disabled")
    if x509_cert_auth and not cbs_ce_version:
        temp_cluster_config = copy_to_temp_conf(cluster_conf, mode)
        persist_cluster_config_environment_prop(temp_cluster_config, 'x509_certs', True)
        persist_cluster_config_environment_prop(temp_cluster_config, 'server_tls_skip_verify', False)
        cluster_conf = temp_cluster_config
    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_config_path=sg_conf)

    # Create clients
    sg_client = MobileRestClient()
    if sync_gateway_version >= "2.5.0":
        sg_admin_url = cluster_topology["sync_gateways"][0]["admin"]
        expvars = sg_client.get_expvars(sg_admin_url, auth=auth)
        chan_cache_misses = expvars["syncgateway"]["per_db"][sg_db]["cache"]["chan_cache_misses"]

    cbs_ip = host_for_url(cbs_url)
    if ssl_enabled and cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify&ipv6=allow".format(cbs_ip)
    elif ssl_enabled and not cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify".format(cbs_ip)
    elif not ssl_enabled and cluster.ipv6:
        connection_url = "couchbase://{}?ipv6=allow".format(cbs_ip)
    else:
        connection_url = 'couchbase://{}'.format(cbs_ip)
    sdk_client = get_cluster(connection_url, bucket_name)
    # Create user / session
    seth_user_info = UserInfo(name='seth', password='pass', channels=['SG', 'SDK'], roles=[])
    sg_client.create_user(
        url=sg_admin_url,
        db=sg_db,
        name=seth_user_info.name,
        password=seth_user_info.password,
        channels=seth_user_info.channels,
        auth=auth
    )

    seth_auth = sg_client.create_session(
        url=sg_admin_url,
        db=sg_db,
        name=seth_user_info.name,
        auth=auth
    )

    # Add docs
    sg_docs = document.create_docs('sg', number=num_docs_per_client, channels=['SG'])
    sg_doc_ids = [doc['_id'] for doc in sg_docs]
    bulk_docs_resp = sg_client.add_bulk_docs(
        url=sg_url,
        db=sg_db,
        docs=sg_docs,
        auth=seth_auth
    )
    assert len(bulk_docs_resp) == num_docs_per_client

    # Stop Sync Gateway
    sg_controller = SyncGateway()
    sg_controller.stop_sync_gateways(cluster_conf, url=sg_url)

    # Update docs that sync gateway wrote via SDK
    sg_docs_via_sdk_get = sdk_client.get_multi(sg_doc_ids)
    assert len(list(sg_docs_via_sdk_get.keys())) == num_docs_per_client
    for doc_id, val in list(sg_docs_via_sdk_get.items()):
        log_info("Updating: '{}' via SDK".format(doc_id))
        doc_body = val.content
        doc_body["updated_by_sdk"] = True
        sdk_client.upsert(doc_id, doc_body)

    # Add additional docs via SDK
    log_info('Adding {} docs via SDK ...'.format(num_docs_per_client))
    sdk_doc_bodies = document.create_docs('sdk', number=num_docs_per_client, channels=['SDK'], non_sgw=True)
    sdk_doc_ids = [doc['id'] for doc in sdk_doc_bodies]
    sdk_docs = {doc['id']: doc for doc in sdk_doc_bodies}
    sdk_docs_resp = []
    for k, v in sdk_docs.items():
        sdk_docs_resp.append(sdk_client.upsert(k, v))

    assert len(sdk_docs_resp) == num_docs_per_client

    # Start Sync Gateway
    sg_controller.start_sync_gateways(cluster_conf, url=sg_url, config=sg_conf)

    # Verify all docs are gettable via Sync Gateway
    all_doc_ids = sg_doc_ids + sdk_doc_ids
    assert len(all_doc_ids) == num_docs_per_client * 2
    bulk_resp, errors = sg_client.get_bulk_docs(url=sg_url, db=sg_db, doc_ids=all_doc_ids, auth=seth_auth)
    assert len(errors) == 0

    # Create a scratch pad and check off docs
    all_doc_ids_scratch_pad = list(all_doc_ids)
    for doc in bulk_resp:
        log_info(doc)
        if doc['_id'].startswith('sg_'):
            # Rev prefix should be '2-' due to the write by Sync Gateway and the update by SDK
            assert doc['_rev'].startswith('2-')
            assert doc['updated_by_sdk']
        else:
            # SDK created doc. Should only have 1 rev from import
            assert doc['_rev'].startswith('1-')
        all_doc_ids_scratch_pad.remove(doc['_id'])
    assert len(all_doc_ids_scratch_pad) == 0

    # Verify all of the docs show up in the changes feed
    docs_to_verify_in_changes = [{'id': doc['_id'], 'rev': doc['_rev']} for doc in bulk_resp]
    sg_client.verify_docs_in_changes(url=sg_url, db=sg_db, expected_docs=docs_to_verify_in_changes, auth=seth_auth)
    if sync_gateway_version >= "2.5.0":
        expvars = sg_client.get_expvars(sg_admin_url, auth=auth)
        assert chan_cache_misses < expvars["syncgateway"]["per_db"][sg_db]["cache"]["chan_cache_misses"], "chan_cache_misses did not get incremented"


@pytest.mark.syncgateway
@pytest.mark.xattrs
@pytest.mark.session
@pytest.mark.parametrize('sg_conf_name', [
    ('sync_gateway_default_functional_tests'),
    ('sync_gateway_default_functional_tests_no_port'),
    pytest.param("sync_gateway_default_functional_tests_couchbase_protocol_withport_11210", marks=pytest.mark.oscertify)
])
def test_large_initial_import(params_from_base_test_setup, sg_conf_name):
    """ Regression test for https://github.com/couchbase/sync_gateway/issues/2537
    Scenario:
    - Stop Sync Gateway
    - Bulk create 30000 docs via SDK
    - Start Sync Gateway to begin import
    - Verify all docs are imported
    """

    num_docs = 30000
    # bucket_name = 'data-bucket'
    sg_db = 'db'

    cluster_conf = params_from_base_test_setup['cluster_config']
    cluster_topology = params_from_base_test_setup['cluster_topology']
    mode = params_from_base_test_setup['mode']
    xattrs_enabled = params_from_base_test_setup['xattrs_enabled']
    ssl_enabled = params_from_base_test_setup["ssl_enabled"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    # Skip the test if ssl disabled as it cannot run without port using http protocol
    if ("sync_gateway_default_functional_tests_no_port" in sg_conf_name) and get_sg_version(cluster_conf) < "1.5.0":
        pytest.skip('couchbase/couchbases ports do not support for versions below 1.5')
    if "sync_gateway_default_functional_tests_no_port" in sg_conf_name and not ssl_enabled:
        pytest.skip('ssl disabled so cannot run without port')

    # Skip the test if ssl enabled as it cannot run using couchbase protocol
    # TODO : https://github.com/couchbaselabs/sync-gateway-accel/issues/227
    # Remove DI condiiton once above bug is fixed
    if "sync_gateway_default_functional_tests_couchbase_protocol_withport_11210" in sg_conf_name and (ssl_enabled or mode.lower() == "di"):
        pytest.skip('ssl enabled so cannot run with couchbase protocol')

    # This test should only run when using xattr meta storage
    if not xattrs_enabled:
        pytest.skip('XATTR tests require --xattrs flag')

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)
    sg_admin_url = cluster_topology['sync_gateways'][0]['admin']
    sg_url = cluster_topology['sync_gateways'][0]['public']
    cbs_url = cluster_topology['couchbase_servers'][0]
    buckets = get_buckets_from_sync_gateway_config(sg_conf, cluster_conf)
    bucket_name = buckets[0]
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None

    log_info('sg_conf: {}'.format(sg_conf))
    log_info('sg_admin_url: {}'.format(sg_admin_url))
    log_info('sg_url: {}'.format(sg_url))
    log_info('cbs_url: {}'.format(cbs_url))

    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_config_path=sg_conf)

    # Stop Sync Gateway
    sg_controller = SyncGateway()
    sg_controller.stop_sync_gateways(cluster_conf, url=sg_url)

    # Connect to server via SDK
    cbs_ip = host_for_url(cbs_url)
    if ssl_enabled and cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify&ipv6=allow".format(cbs_ip)
    elif ssl_enabled and not cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify".format(cbs_ip)
    elif not ssl_enabled and cluster.ipv6:
        connection_url = "couchbase://{}?ipv6=allow".format(cbs_ip)
    else:
        connection_url = 'couchbase://{}'.format(cbs_ip)
    bucket_cluster = get_cluster(connection_url, bucket_name)
    # Generate array for each doc doc to give it a larger size

    def prop_gen():
        return {'sample_array': ["test_item_{}".format(i) for i in range(20)]}

    # Create 'num_docs' docs from SDK
    sdk_doc_bodies = document.create_docs('sdk', num_docs, channels=['created_via_sdk'], prop_generator=prop_gen, non_sgw=True)
    sdk_doc_ids = [doc['id'] for doc in sdk_doc_bodies]
    assert len(sdk_doc_ids) == num_docs
    sdk_docs = {doc['id']: doc for doc in sdk_doc_bodies}
    for k, v in sdk_docs.items():
        bucket_cluster.upsert(k, v)

    # Start Sync Gateway to begin import
    sg_controller.start_sync_gateways(cluster_conf, url=sg_url, config=sg_conf)

    # Let some documents process
    log_info('Sleeping 30s to let some docs auto import')
    time.sleep(30)

    # Any document that have not been imported with be imported on demand.
    # Verify that all the douments have been imported
    sg_client = MobileRestClient()
    seth_auth = sg_client.create_user(url=sg_admin_url, db=sg_db, name='seth', password='pass', channels=['created_via_sdk'], auth=auth)

    sdk_doc_ids_scratch_pad = list(sdk_doc_ids)
    bulk_resp, errors = sg_client.get_bulk_docs(url=sg_url, db=sg_db, doc_ids=sdk_doc_ids, auth=seth_auth)
    assert len(errors) == 0

    for doc in bulk_resp:
        log_info('Doc: {}'.format(doc))
        assert doc['_rev'].startswith('1-')
        sdk_doc_ids_scratch_pad.remove(doc['_id'])

    assert len(sdk_doc_ids_scratch_pad) == 0

    # Verify all of the docs show up in the changes feed
    docs_to_verify_in_changes = [{'id': doc['_id'], 'rev': doc['_rev']} for doc in bulk_resp]
    sg_client.verify_docs_in_changes(url=sg_url, db=sg_db, expected_docs=docs_to_verify_in_changes, auth=seth_auth)


@pytest.mark.syncgateway
@pytest.mark.xattrs
@pytest.mark.session
@pytest.mark.parametrize('sg_conf_name, use_multiple_channels, x509_cert_auth', [
    ('sync_gateway_default_functional_tests', False, True),
    ('sync_gateway_default_functional_tests', True, False),
    pytest.param('sync_gateway_default_functional_tests_no_port', False, True, marks=pytest.mark.oscertify),
    ('sync_gateway_default_functional_tests_no_port', True, False)
])
def test_purge(params_from_base_test_setup, sg_conf_name, use_multiple_channels, x509_cert_auth):
    """
    Scenario:
    - Bulk create 1000 docs via Sync Gateway
    - Bulk create 1000 docs via SDK
    - Get all of the docs via Sync Gateway
    - Get all of the docs via SDK
    - Sync Gateway delete 1/2 the docs, This will exercise purge on deleted and non-deleted docs
    - Sync Gateway purge all docs
    - Verify SDK can't see the docs
    - Verify SG can't see the docs
    - Verify XATTRS are gone using SDK client with full bucket permissions via subdoc?
    """

    cluster_conf = params_from_base_test_setup['cluster_config']
    cluster_topology = params_from_base_test_setup['cluster_topology']
    mode = params_from_base_test_setup['mode']
    xattrs_enabled = params_from_base_test_setup['xattrs_enabled']
    ssl_enabled = params_from_base_test_setup["ssl_enabled"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if ("sync_gateway_default_functional_tests_no_port" in sg_conf_name) and get_sg_version(cluster_conf) < "1.5.0":
        pytest.skip('couchbase/couchbases ports do not support for versions below 1.5')
    if "sync_gateway_default_functional_tests_no_port" in sg_conf_name and not ssl_enabled:
        pytest.skip('ssl disabled so cannot run without port')

    # This test should only run when using xattr meta storage
    if not xattrs_enabled:
        pytest.skip('XATTR tests require --xattrs flag')

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)
    sg_admin_url = cluster_topology['sync_gateways'][0]['admin']
    sg_url = cluster_topology['sync_gateways'][0]['public']
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None

    # bucket_name = 'data-bucket'
    buckets = get_buckets_from_sync_gateway_config(sg_conf, cluster_conf)
    bucket_name = buckets[0]
    cbs_url = cluster_topology['couchbase_servers'][0]
    sg_db = 'db'
    number_docs_per_client = 10
    number_revs_per_doc = 1

    if use_multiple_channels:
        log_info('Using multiple channels')
        channels = ['shared_channel_{}'.format(i) for i in range(1000)]
    else:
        log_info('Using a single channel')
        channels = ['NASA']

    log_info('sg_conf: {}'.format(sg_conf))
    log_info('sg_admin_url: {}'.format(sg_admin_url))
    log_info('sg_url: {}'.format(sg_url))
    disable_tls_server = params_from_base_test_setup["disable_tls_server"]
    if x509_cert_auth and disable_tls_server:
        pytest.skip("x509 test cannot run tls server disabled")
    if x509_cert_auth:
        temp_cluster_config = copy_to_temp_conf(cluster_conf, mode)
        persist_cluster_config_environment_prop(temp_cluster_config, 'x509_certs', True)
        persist_cluster_config_environment_prop(temp_cluster_config, 'server_tls_skip_verify', False)
        cluster_conf = temp_cluster_config
    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_config_path=sg_conf)

    sg_client = MobileRestClient()
    seth_user_info = UserInfo(name='seth', password='pass', channels=channels, roles=[])
    sg_client.create_user(
        url=sg_admin_url,
        db=sg_db,
        name=seth_user_info.name,
        password=seth_user_info.password,
        channels=seth_user_info.channels,
        auth=auth
    )

    seth_auth = sg_client.create_session(
        url=sg_admin_url,
        db=sg_db,
        name=seth_user_info.name,
        auth=auth
    )

    # Create 'number_docs_per_client' docs from Sync Gateway
    seth_docs = document.create_docs('sg', number=number_docs_per_client, channels=seth_user_info.channels)
    bulk_docs_resp = sg_client.add_bulk_docs(
        url=sg_url,
        db=sg_db,
        docs=seth_docs,
        auth=seth_auth
    )
    assert len(bulk_docs_resp) == number_docs_per_client

    # Connect to server via SDK
    cbs_ip = host_for_url(cbs_url)
    if ssl_enabled and cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify&ipv6=allow".format(cbs_ip)
    elif ssl_enabled and not cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify".format(cbs_ip)
    elif not ssl_enabled and cluster.ipv6:
        connection_url = "couchbase://{}?ipv6=allow".format(cbs_ip)
    else:
        connection_url = 'couchbase://{}'.format(cbs_ip)
    sdk_client = get_cluster(connection_url, bucket_name)
    # Create 'number_docs_per_client' docs from SDK
    sdk_doc_bodies = document.create_docs('sdk', number_docs_per_client, channels=seth_user_info.channels, non_sgw=True)
    sdk_docs = {doc['id']: doc for doc in sdk_doc_bodies}
    sdk_doc_ids = [doc for doc in sdk_docs]
    for k, v in sdk_docs.items():
        sdk_client.upsert(k, v)

    sg_doc_ids = ['sg_{}'.format(i) for i in range(number_docs_per_client)]
    sdk_doc_ids = ['sdk_{}'.format(i) for i in range(number_docs_per_client)]
    all_doc_ids = sg_doc_ids + sdk_doc_ids

    # Get all of the docs via Sync Gateway
    sg_docs, errors = sg_client.get_bulk_docs(url=sg_url, db=sg_db, doc_ids=all_doc_ids, auth=seth_auth)
    assert len(sg_docs) == number_docs_per_client * 2
    assert len(errors) == 0

    # Check that all of the doc ids are present in the SG response
    doc_id_scatch_pad = list(all_doc_ids)
    assert len(doc_id_scatch_pad) == number_docs_per_client * 2
    for sg_doc in sg_docs:
        log_info('Found doc through SG: {}'.format(sg_doc['_id']))
        doc_id_scatch_pad.remove(sg_doc['_id'])
    assert len(doc_id_scatch_pad) == 0

    # Get all of the docs via SDK
    sdk_docs = sdk_client.get_multi(all_doc_ids)
    assert len(sdk_docs) == number_docs_per_client * 2

    # Verify XATTRS present via SDK and SG and that they are the same
    for doc_id in all_doc_ids:
        verify_sg_xattrs(
            mode,
            sg_client,
            sg_url=sg_admin_url,
            sg_db=sg_db,
            doc_id=doc_id,
            expected_number_of_revs=number_revs_per_doc,
            expected_number_of_channels=len(channels),
            auth=auth
        )

    # Check that all of the doc ids are present in the SDK response
    doc_id_scatch_pad = list(all_doc_ids)
    assert len(doc_id_scatch_pad) == number_docs_per_client * 2
    for sdk_doc in sdk_docs:
        log_info('Found doc through SDK: {}'.format(sdk_doc))
        doc_id_scatch_pad.remove(sdk_doc)
    assert len(doc_id_scatch_pad) == 0

    # Use Sync Gateway to delete half of the documents choosen randomly
    deletion_count = 0
    doc_id_choice_pool = list(all_doc_ids)
    deleted_doc_ids = []
    while deletion_count < number_docs_per_client:

        # Get a random doc_id from available doc ids
        random_doc_id = random.choice(doc_id_choice_pool)

        # Get the current revision of the doc and delete it
        doc = sg_client.get_doc(url=sg_url, db=sg_db, doc_id=random_doc_id, auth=seth_auth)
        sg_client.delete_doc(url=sg_url, db=sg_db, doc_id=random_doc_id, rev=doc['_rev'], auth=seth_auth)

        # Remove deleted doc from pool of choices
        doc_id_choice_pool.remove(random_doc_id)
        deleted_doc_ids.append(random_doc_id)
        deletion_count += 1

    # Verify xattrs still exist on deleted docs
    # Expected revs will be + 1 due to the deletion revision
    for doc_id in deleted_doc_ids:
        verify_sg_xattrs(
            mode,
            sg_client,
            sg_url=sg_admin_url,
            sg_db=sg_db,
            doc_id=doc_id,
            expected_number_of_revs=number_revs_per_doc + 1,
            expected_number_of_channels=len(channels),
            deleted_docs=True,
            auth=auth
        )

    assert len(doc_id_choice_pool) == number_docs_per_client
    assert len(deleted_doc_ids) == number_docs_per_client

    # Sync Gateway purge all docs
    sg_client.purge_docs(url=sg_admin_url, db=sg_db, docs=sg_docs, auth=auth)

    # Verify SG can't see the docs. Bulk get should only return errors
    sg_docs_visible_after_purge, errors = sg_client.get_bulk_docs(url=sg_url, db=sg_db, doc_ids=all_doc_ids, auth=seth_auth, validate=False)
    assert len(sg_docs_visible_after_purge) == 0
    assert len(errors) == number_docs_per_client * 2

    # Verify that all docs have been deleted
    sg_deleted_doc_scratch_pad = list(all_doc_ids)
    for error in errors:
        assert error['status'] == 404
        assert error['reason'] == 'missing'
        assert error['error'] == 'not_found'
        assert error['id'] in sg_deleted_doc_scratch_pad
        sg_deleted_doc_scratch_pad.remove(error['id'])
    assert len(sg_deleted_doc_scratch_pad) == 0

    # Verify SDK can't see the docs
    sdk_deleted_doc_scratch_pad = list(all_doc_ids)
    for doc_id in all_doc_ids:
        nfe = None
        with pytest.raises(DocumentNotFoundException) as nfe:
            sdk_client.get(doc_id)
        log_info(nfe.value)
        if nfe is not None:
            sdk_deleted_doc_scratch_pad.remove(nfe.value.key)
    assert len(sdk_deleted_doc_scratch_pad) == 0

    # Verify XATTRS are gone using SDK client with full bucket permissions via subdoc?
    for doc_id in all_doc_ids:
        verify_no_sg_xattrs(
            sg_client=sg_client,
            sg_url=sg_url,
            sg_db=sg_db,
            doc_id=doc_id
        )


@pytest.mark.syncgateway
@pytest.mark.xattrs
@pytest.mark.session
@pytest.mark.parametrize('sg_conf_name', [
    pytest.param('sync_gateway_default_functional_tests', marks=pytest.mark.oscertify),
    ('sync_gateway_default_functional_tests_no_port'),
    ('sync_gateway_default_functional_tests_couchbase_protocol_withport_11210')
])
def test_sdk_does_not_see_sync_meta(params_from_base_test_setup, sg_conf_name):
    """
    Scenario:
    - Bulk create 1000 docs via sync gateway
    - Perform GET of docs from SDK
    - Assert that SDK does not see any sync meta data
    """

    cluster_conf = params_from_base_test_setup['cluster_config']
    cluster_topology = params_from_base_test_setup['cluster_topology']
    mode = params_from_base_test_setup['mode']
    xattrs_enabled = params_from_base_test_setup['xattrs_enabled']
    ssl_enabled = params_from_base_test_setup["ssl_enabled"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    # Skip the test if ssl disabled as it cannot run without port using http protocol
    if ("sync_gateway_default_functional_tests_no_port" in sg_conf_name) and get_sg_version(cluster_conf) < "1.5.0":
        pytest.skip('couchbase/couchbases ports do not support for versions below 1.5')
    if "sync_gateway_default_functional_tests_no_port" in sg_conf_name and not ssl_enabled:
        pytest.skip('ssl disabled so cannot run without port')

    # Skip the test if ssl enabled as it cannot run using couchbase protocol
    # TODO : https://github.com/couchbaselabs/sync-gateway-accel/issues/227
    # Remove DI condiiton once above bug is fixed
    if "sync_gateway_default_functional_tests_couchbase_protocol_withport_11210" in sg_conf_name and (ssl_enabled or mode.lower() == "di"):
        pytest.skip('ssl enabled so cannot run with couchbase protocol')

    # This test should only run when using xattr meta storage
    if not xattrs_enabled:
        pytest.skip('XATTR tests require --xattrs flag')

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)
    sg_admin_url = cluster_topology['sync_gateways'][0]['admin']
    sg_url = cluster_topology['sync_gateways'][0]['public']
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None

    # bucket_name = 'data-bucket'
    buckets = get_buckets_from_sync_gateway_config(sg_conf, cluster_conf)
    bucket_name = buckets[0]
    cbs_url = cluster_topology['couchbase_servers'][0]
    sg_db = 'db'
    number_of_sg_docs = 1000
    channels = ['NASA']

    log_info('sg_conf: {}'.format(sg_conf))
    log_info('sg_admin_url: {}'.format(sg_admin_url))
    log_info('sg_url: {}'.format(sg_url))

    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_config_path=sg_conf)

    # Create sg user
    sg_client = MobileRestClient()
    sg_client.create_user(url=sg_admin_url, db=sg_db, name='seth', password='pass', channels=['shared'], auth=auth)
    seth_session = sg_client.create_session(url=sg_admin_url, db=sg_db, name='seth', auth=auth)

    # Connect to server via SDK
    cbs_ip = host_for_url(cbs_url)
    if ssl_enabled and cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify&ipv6=allow".format(cbs_ip)
    elif ssl_enabled and not cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify".format(cbs_ip)
    elif not ssl_enabled and cluster.ipv6:
        connection_url = "couchbase://{}?ipv6=allow".format(cbs_ip)
    else:
        connection_url = 'couchbase://{}'.format(cbs_ip)
    sdk_client = get_cluster(connection_url, bucket_name)
    # Add 'number_of_sg_docs' to Sync Gateway
    sg_doc_bodies = document.create_docs(
        doc_id_prefix='sg_docs',
        number=number_of_sg_docs,
        attachments_generator=attachment.generate_2_png_100_100,
        channels=channels
    )
    sg_bulk_resp = sg_client.add_bulk_docs(url=sg_url, db=sg_db, docs=sg_doc_bodies, auth=seth_session)
    assert len(sg_bulk_resp) == number_of_sg_docs

    doc_ids = ['sg_docs_{}'.format(i) for i in range(number_of_sg_docs)]

    # Get all of the docs via the SDK
    docs_from_sg = sdk_client.get_multi(doc_ids)
    assert len(docs_from_sg) == number_of_sg_docs, "sg docs and docs from sdk has mismatch"

    attachment_name_ids = []
    for doc_key, doc_val in list(docs_from_sg.items()):
        # Scratch doc off in list of all doc ids
        doc_ids.remove(doc_key)

        # Get the document body
        doc_body = doc_val.content

        # Make sure 'sync' property is not present in the document
        assert '_sync' not in doc_body

        # Build tuple of the filename and server doc id of the attachments
        if sync_gateway_version < "2.5":
            for att_key, att_val in list(doc_body['_attachments'].items()):
                if sync_gateway_version >= "3.0.0":
                    attachment_name_ids.append((att_key, '_sync:att2:{}'.format(att_val['digest'])))
                else:
                    attachment_name_ids.append((att_key, '_sync:att:{}'.format(att_val['digest'])))

    assert len(doc_ids) == 0

    # Verify attachments stored locally have the same data as those written to the server
    if sync_gateway_version < "2.5":
        for att_file_name, att_doc_id in attachment_name_ids:

            att_doc = sdk_client.get(att_doc_id, no_format=True)
            att_bytes = att_doc.content

            local_file_path = '{}/{}'.format(DATA_DIR, att_file_name)
            log_info('Checking that the generated attachment is the same that is store on server: {}'.format(
                local_file_path
            ))
            with open(local_file_path, 'rb') as local_file:
                local_bytes = local_file.read()
                assert att_bytes == local_bytes


@pytest.mark.syncgateway
@pytest.mark.xattrs
@pytest.mark.session
@pytest.mark.parametrize('sg_conf_name', [
    ('sync_gateway_default_functional_tests'),
    pytest.param('sync_gateway_default_functional_tests_no_port', marks=pytest.mark.oscertify),
    ('sync_gateway_default_functional_tests_couchbase_protocol_withport_11210')
])
def test_sg_sdk_interop_unique_docs(params_from_base_test_setup, sg_conf_name):
    """
    Scenario:
    - Bulk create 'number_docs' docs from SDK with id prefix 'sdk' and channels ['sdk']
    - Bulk create 'number_docs' docs from SG with id prefix 'sg' and channels ['sg']
    - SDK: Verify docs (sg + sdk) are present
    - SG: Verify docs (sg + sdk) are there via _all_docs
    - SG: Verify docs (sg + sdk) are there via _changes
    - Bulk update each doc 'number_updates' from SDK for 'sdk' docs
    - SDK should verify it does not see _sync
    - Bulk update each doc 'number_updates' from SG for 'sg' docs
    - SDK: Verify doc updates (sg + sdk) are present using the doc['content']['updates'] property
    - SG: Verify doc updates (sg + sdk) are there via _all_docs using the doc['content']['updates'] property and rev prefix
    - SG: Verify doc updates (sg + sdk) are there via _changes using the doc['content']['updates'] property and rev prefix
    - SDK should verify it does not see _sync
    - Bulk delete 'sdk' docs from SDK
    - Bulk delete 'sg' docs from SG
    - Verify SDK sees all docs (sg + sdk) as deleted
    - Verify SG sees all docs (sg + sdk) as deleted
    """

    cluster_conf = params_from_base_test_setup['cluster_config']
    cluster_topology = params_from_base_test_setup['cluster_topology']
    mode = params_from_base_test_setup['mode']
    xattrs_enabled = params_from_base_test_setup['xattrs_enabled']
    ssl_enabled = params_from_base_test_setup["ssl_enabled"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    # Skip the test if ssl disabled as it cannot run without port using http protocol
    if ("sync_gateway_default_functional_tests_no_port" in sg_conf_name) and get_sg_version(cluster_conf) < "1.5.0":
        pytest.skip('couchbase/couchbases ports do not support for versions below 1.5')
    if "sync_gateway_default_functional_tests_no_port" in sg_conf_name and not ssl_enabled:
        pytest.skip('ssl disabled so cannot run without port')

    # Skip the test if ssl enabled as it cannot run using couchbase protocol
    # TODO : https://github.com/couchbaselabs/sync-gateway-accel/issues/227
    # Remove DI condiiton once above bug is fixed
    if "sync_gateway_default_functional_tests_couchbase_protocol_withport_11210" in sg_conf_name and (ssl_enabled or mode.lower() == "di"):
        pytest.skip('ssl enabled so cannot run with couchbase protocol')

    # This test should only run when using xattr meta storage
    if not xattrs_enabled:
        pytest.skip('XATTR tests require --xattrs flag')

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)
    sg_admin_url = cluster_topology['sync_gateways'][0]['admin']
    sg_url = cluster_topology['sync_gateways'][0]['public']
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None

    # bucket_name = 'data-bucket'
    buckets = get_buckets_from_sync_gateway_config(sg_conf, cluster_conf)
    bucket_name = buckets[0]
    cbs_url = cluster_topology['couchbase_servers'][0]
    sg_db = 'db'
    number_docs_per_client = 10
    number_updates = 10

    log_info('sg_conf: {}'.format(sg_conf))
    log_info('sg_admin_url: {}'.format(sg_admin_url))
    log_info('sg_url: {}'.format(sg_url))

    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_config_path=sg_conf)

    # Connect to server via SDK
    log_info('Connecting to bucket ...')
    cbs_ip = host_for_url(cbs_url)
    if ssl_enabled and cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify&ipv6=allow".format(cbs_ip)
    elif ssl_enabled and not cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify".format(cbs_ip)
    elif not ssl_enabled and cluster.ipv6:
        connection_url = "couchbase://{}?ipv6=allow".format(cbs_ip)
    else:
        connection_url = 'couchbase://{}'.format(cbs_ip)
    sdk_client = get_cluster(connection_url, bucket_name)

    # Create docs and add them via sdk
    log_info('Adding docs via sdk ...')
    sdk_doc_bodies = document.create_docs('sdk', number_docs_per_client, content={'foo': 'bar', 'updates': 1}, channels=['sdk'], non_sgw=True)
    sdk_docs = {doc['id']: doc for doc in sdk_doc_bodies}
    sdk_doc_ids = [doc for doc in sdk_docs]
    for k, v in sdk_docs.items():
        sdk_client.upsert(k, v)

    # Create sg user
    log_info('Creating user / session on Sync Gateway ...')
    sg_client = MobileRestClient()
    sg_client.create_user(url=sg_admin_url, db=sg_db, name='seth', password='pass', channels=['sg', 'sdk'], auth=auth)
    seth_session = sg_client.create_session(url=sg_admin_url, db=sg_db, name='seth', auth=auth)

    # Create / add docs to sync gateway
    log_info('Adding docs Sync Gateway ...')
    sg_docs = document.create_docs('sg', number_docs_per_client, content={'foo': 'bar', 'updates': 1}, channels=['sg'])
    log_info('Adding bulk_docs')
    sg_docs_resp = sg_client.add_bulk_docs(url=sg_url, db=sg_db, docs=sg_docs, auth=seth_session)
    sg_doc_ids = [doc['_id'] for doc in sg_docs]
    assert len(sg_docs_resp) == number_docs_per_client

    all_doc_ids = sdk_doc_ids + sg_doc_ids

    # Verify docs all docs are present via SG _bulk_get
    log_info('Verify Sync Gateway sees all docs via _bulk_get ...')
    all_docs_via_sg, errors = sg_client.get_bulk_docs(url=sg_url, db=sg_db, doc_ids=all_doc_ids, auth=seth_session)
    assert len(all_docs_via_sg) == number_docs_per_client * 2
    assert len(errors) == 0
    verify_doc_ids_in_sg_bulk_response(all_docs_via_sg, number_docs_per_client * 2, all_doc_ids)

    # Verify docs all docs are present via SG _all_docs
    log_info('Verify Sync Gateway sees all docs via _all_docs ...')
    all_docs_resp = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=seth_session)
    verify_doc_ids_in_sg_all_docs_response(all_docs_resp, number_docs_per_client * 2, all_doc_ids)

    # Verify docs all docs are present via SDK get_multi
    log_info('Verify SDK sees all docs via get_multi ...')
    all_docs_via_sdk = sdk_client.get_multi(all_doc_ids)
    verify_doc_ids_in_sdk_get_multi(all_docs_via_sdk, number_docs_per_client * 2, all_doc_ids)

    # SG: Verify docs (sg + sdk) are there via _changes
    # Format docs for changes verification
    log_info('Verify Sync Gateway sees all docs on _changes ...')
    all_docs_via_sg_formatted = [{"id": doc["_id"], "rev": doc["_rev"]} for doc in all_docs_via_sg]
    assert len(all_docs_via_sg_formatted) == number_docs_per_client * 2
    sg_client.verify_docs_in_changes(url=sg_url, db=sg_db, expected_docs=all_docs_via_sg_formatted, auth=seth_session)

    log_info("Sync Gateway updates 'sg_*' docs and SDK updates 'sdk_*' docs ...")
    for i in range(number_updates):

        # Get docs and extract doc_id (key) and doc_body (value.value)
        sdk_docs_resp = sdk_client.get_multi(sdk_doc_ids)
        docs = {k: v.content for k, v in list(sdk_docs_resp.items())}

        # update the updates property for every doc
        for _, v in list(docs.items()):
            v['content']['updates'] = 1 + v['content']['updates']

        log_info(sdk_client.upsert_multi(docs))
        time.sleep(13)

        # Get docs from Sync Gateway
        sg_docs_to_update, errors = sg_client.get_bulk_docs(url=sg_url, db=sg_db, doc_ids=sg_doc_ids, auth=seth_session)
        assert len(sg_docs_to_update) == number_docs_per_client
        assert len(errors) == 0

        # Update the docs
        for sg_doc in sg_docs_to_update:
            sg_doc['content']['updates'] = 1 + sg_doc['content']['updates']

        # Bulk add the updates to Sync Gateway
        sg_docs_resp = sg_client.add_bulk_docs(url=sg_url, db=sg_db, docs=sg_docs_to_update, auth=seth_session)

    # Verify updates from SG via _bulk_get
    log_info('Verify Sync Gateway sees all docs via _bulk_get ...')
    docs_from_sg_bulk_get, errors = sg_client.get_bulk_docs(url=sg_url, db=sg_db, doc_ids=all_doc_ids, auth=seth_session)
    assert len(docs_from_sg_bulk_get) == number_docs_per_client * 2
    assert len(errors) == 0
    for doc in docs_from_sg_bulk_get:
        # If it is an SG doc the revision prefix should match the number of updates.
        # This may not be the case due to batched importing of SDK updates
        if doc['_id'].startswith('sg_'):
            assert doc['_rev'].startswith('{}-'.format(number_updates + 1))
        assert doc['content']['updates'] == number_updates + 1

    # Verify updates from SG via _all_docs
    log_info('Verify Sync Gateway sees updates via _all_docs ...')
    docs_from_sg_all_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=seth_session, include_docs=True)
    assert len(docs_from_sg_all_docs['rows']) == number_docs_per_client * 2
    for doc in docs_from_sg_all_docs['rows']:
        # If it is an SG doc the revision prefix should match the number of updates.
        # This may not be the case due to batched importing of SDK updates
        if doc['id'].startswith('sg_'):
            assert doc['value']['rev'].startswith('{}-'.format(number_updates + 1))
            assert doc['doc']['_rev'].startswith('{}-'.format(number_updates + 1))

        assert doc['id'] == doc['doc']['_id']
        assert doc['doc']['content']['updates'] == number_updates + 1

    # Verify updates from SG via _changes
    log_info('Verify Sync Gateway sees updates on _changes ...')
    all_docs_via_sg_formatted = [{"id": doc["_id"], "rev": doc["_rev"]} for doc in docs_from_sg_bulk_get]
    sg_client.verify_docs_in_changes(url=sg_url, db=sg_db, expected_docs=all_docs_via_sg_formatted, auth=seth_session)

    # Verify updates from SDK via get_multi
    log_info('Verify SDK sees updates via get_multi ...')
    all_docs_from_sdk = sdk_client.get_multi(all_doc_ids)
    assert len(all_docs_from_sdk) == number_docs_per_client * 2
    for doc_id, value in list(all_docs_from_sdk.items()):
        assert '_sync' not in value.content
        assert value.content['content']['updates'] == number_updates + 1

    # Delete the sync gateway docs
    log_info("Deleting 'sg_*' docs from Sync Gateway  ...")
    sg_docs_to_delete, errors = sg_client.get_bulk_docs(url=sg_url, db=sg_db, doc_ids=sg_doc_ids, auth=seth_session)
    assert len(sg_docs_to_delete) == number_docs_per_client
    assert len(errors) == 0

    sg_docs_delete_resp = sg_client.delete_bulk_docs(url=sg_url, db=sg_db, docs=sg_docs_to_delete, auth=seth_session)
    assert len(sg_docs_delete_resp) == number_docs_per_client

    # Delete the sdk docs
    log_info("Deleting 'sdk_*' docs from SDK  ...")
    sdk_client.remove_multi(sdk_doc_ids)

    # Verify all docs are deleted on the sync_gateway side
    all_doc_ids = sdk_doc_ids + sg_doc_ids
    assert len(all_doc_ids) == 2 * number_docs_per_client

    # Check deletes via GET /db/doc_id and bulk_get
    verify_sg_deletes(client=sg_client, url=sg_url, db=sg_db, docs_to_verify_deleted=all_doc_ids, auth=seth_session)

    # Verify all docs are deleted on sdk, deleted docs should rase and exception
    sdk_doc_delete_scratch_pad = list(all_doc_ids)
    for doc_id in all_doc_ids:
        nfe = None
        with pytest.raises(DocumentNotFoundException) as nfe:
            sdk_client.get(doc_id)
        log_info(nfe.value)
        if nfe is not None:
            sdk_doc_delete_scratch_pad.remove(nfe.value.key)

    # Assert that all of the docs are flagged as deleted
    assert len(sdk_doc_delete_scratch_pad) == 0


@pytest.mark.syncgateway
@pytest.mark.xattrs
@pytest.mark.session
@pytest.mark.parametrize(
    'sg_conf_name, number_docs_per_client, number_updates_per_doc_per_client',
    [
        ('sync_gateway_default_functional_tests', 10, 10),
        ('sync_gateway_default_functional_tests', 100, 10),
        ('sync_gateway_default_functional_tests_no_port', 100, 10),
        pytest.param('sync_gateway_default_functional_tests', 10, 100, marks=pytest.mark.oscertify),
        ('sync_gateway_default_functional_tests_no_port', 10, 100),
        ('sync_gateway_default_functional_tests', 1, 1000)
    ]
)
def test_sg_sdk_interop_shared_docs(params_from_base_test_setup,
                                    sg_conf_name,
                                    number_docs_per_client,
                                    number_updates_per_doc_per_client):
    """
    Scenario:
    - Bulk create 'number_docs' docs from SDK with prefix 'doc_set_one' and channels ['shared']
      with 'sg_one_updates' and 'sdk_one_updates' counter properties
    - Bulk create 'number_docs' docs from SG with prefix 'doc_set_two' and channels ['shared']
      with 'sg_one_updates' and 'sdk_one_updates' counter properties
    - SDK: Verify docs (sg + sdk) are present
    - SG: Verify docs (sg + sdk) are there via _all_docs
    - SG: Verify docs (sg + sdk) are there via _changes
    - Start concurrent updates:
        - Start update from sg / sdk to a shared set of docs. Sync Gateway and SDK will try to update
          random docs from the shared set and update the corresponding counter property as well as the
          'updates' properties
    - SDK: Verify doc updates (sg + sdk) are present using the counter properties
    - SG: Verify doc updates (sg + sdk) are there via _changes using the counter properties and rev prefix
    - Start concurrent deletes:
        loop until len(all_doc_ids_to_delete) == 0
            - List of all_doc_ids_to_delete
            - Pick random doc and try to delete from sdk
            - If successful, remove from list
            - Pick random doc and try to delete from sg
            - If successful, remove from list
    - Verify SDK sees all docs (sg + sdk) as deleted
    - Verify SG sees all docs (sg + sdk) as deleted
    """

    cluster_conf = params_from_base_test_setup['cluster_config']
    cluster_topology = params_from_base_test_setup['cluster_topology']
    mode = params_from_base_test_setup['mode']
    xattrs_enabled = params_from_base_test_setup['xattrs_enabled']
    ssl_enabled = params_from_base_test_setup["ssl_enabled"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if ("sync_gateway_default_functional_tests_no_port" in sg_conf_name) and get_sg_version(cluster_conf) < "1.5.0":
        pytest.skip('couchbase/couchbases ports do not support for versions below 1.5')
    if "sync_gateway_default_functional_tests_no_port" in sg_conf_name and not ssl_enabled:
        pytest.skip('ssl disabled so cannot run without port')

    # This test should only run when using xattr meta storage
    if not xattrs_enabled:
        pytest.skip('XATTR tests require --xattrs flag')

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)
    sg_admin_url = cluster_topology['sync_gateways'][0]['admin']
    sg_url = cluster_topology['sync_gateways'][0]['public']
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None

    # bucket_name = 'data-bucket'
    buckets = get_buckets_from_sync_gateway_config(sg_conf, cluster_conf)
    bucket_name = buckets[0]
    cbs_url = cluster_topology['couchbase_servers'][0]
    sg_db = 'db'

    log_info('Num docs per client: {}'.format(number_docs_per_client))
    log_info('Num updates per doc per client: {}'.format(number_updates_per_doc_per_client))

    log_info('sg_conf: {}'.format(sg_conf))
    log_info('sg_admin_url: {}'.format(sg_admin_url))
    log_info('sg_url: {}'.format(sg_url))

    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_config_path=sg_conf)

    sg_tracking_prop = 'sg_one_updates'
    sdk_tracking_prop = 'sdk_one_updates'

    # Create sg user
    sg_client = MobileRestClient()
    sg_client.create_user(url=sg_admin_url, db=sg_db, name='seth', password='pass', channels=['shared'], auth=auth)
    seth_session = sg_client.create_session(url=sg_admin_url, db=sg_db, name='seth', auth=auth)

    # Connect to server via SDK
    cbs_ip = host_for_url(cbs_url)
    if ssl_enabled and cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify&ipv6=allow".format(cbs_ip)
    elif ssl_enabled and not cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify".format(cbs_ip)
    elif not ssl_enabled and cluster.ipv6:
        connection_url = "couchbase://{}?ipv6=allow".format(cbs_ip)
    else:
        connection_url = 'couchbase://{}'.format(cbs_ip)
    sdk_client = get_cluster(connection_url, bucket_name)

    # Inject custom properties into doc template
    def update_props():
        return {
            'updates': 0,
            sg_tracking_prop: 0,
            sdk_tracking_prop: 0
        }

    # Create / add docs to sync gateway
    sg_docs = document.create_docs(
        'doc_set_one',
        number_docs_per_client,
        channels=['shared'],
        prop_generator=update_props
    )
    sg_docs_resp = sg_client.add_bulk_docs(
        url=sg_url,
        db=sg_db,
        docs=sg_docs,
        auth=seth_session
    )
    doc_set_one_ids = [doc['_id'] for doc in sg_docs]
    assert len(sg_docs_resp) == number_docs_per_client

    # Create / add docs via sdk
    sdk_doc_bodies = document.create_docs(
        'doc_set_two',
        number_docs_per_client,
        channels=['shared'],
        prop_generator=update_props,
        non_sgw=True
    )

    # Add docs via SDK
    log_info('Adding {} docs via SDK ...'.format(number_docs_per_client))
    sdk_docs = {doc['id']: doc for doc in sdk_doc_bodies}
    doc_set_two_ids = [sdk_doc['id'] for sdk_doc in sdk_doc_bodies]
    sdk_docs_resp = []
    for k, v in sdk_docs.items():
        sdk_docs_resp.append(sdk_client.upsert(k, v))
    assert len(sdk_docs_resp) == number_docs_per_client

    # Build list of all doc_ids
    all_docs_ids = doc_set_one_ids + doc_set_two_ids
    assert len(all_docs_ids) == number_docs_per_client * 2

    # Verify docs (sg + sdk) via SG bulk_get
    docs_from_sg_bulk_get, errors = sg_client.get_bulk_docs(url=sg_url, db=sg_db, doc_ids=all_docs_ids, auth=seth_session)
    assert len(docs_from_sg_bulk_get) == number_docs_per_client * 2
    assert len(errors) == 0
    verify_doc_ids_in_sg_bulk_response(docs_from_sg_bulk_get, number_docs_per_client * 2, all_docs_ids)

    # Verify docs (sg + sdk) are there via _all_docs
    all_docs_resp = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=seth_session)
    assert len(all_docs_resp["rows"]) == number_docs_per_client * 2
    verify_doc_ids_in_sg_all_docs_response(all_docs_resp, number_docs_per_client * 2, all_docs_ids)

    # SG: Verify docs (sg + sdk) are there via _changes
    all_docs_via_sg_formatted = [{"id": doc["_id"], "rev": doc["_rev"]} for doc in docs_from_sg_bulk_get]
    sg_client.verify_docs_in_changes(url=sg_url, db=sg_db, expected_docs=all_docs_via_sg_formatted, auth=seth_session)

    # SDK: Verify docs (sg + sdk) are present
    all_docs_via_sdk = sdk_client.get_multi(all_docs_ids)
    verify_doc_ids_in_sdk_get_multi(all_docs_via_sdk, number_docs_per_client * 2, all_docs_ids)

    # Build a dictionary of all the doc ids with default number of updates (1 for created)
    all_doc_ids = doc_set_one_ids + doc_set_two_ids
    assert len(all_doc_ids) == number_docs_per_client * 2

    # Update the same documents concurrently from a sync gateway client and and sdk client
    with ThreadPoolExecutor(max_workers=5) as tpe:
        update_from_sg_task = tpe.submit(
            update_sg_docs,
            client=sg_client,
            url=sg_url,
            db=sg_db,
            docs_to_update=all_doc_ids,
            prop_to_update=sg_tracking_prop,
            number_updates=number_updates_per_doc_per_client,
            auth=seth_session
        )

        update_from_sdk_task = tpe.submit(
            update_sdk_docs,
            client=sdk_client,
            docs_to_update=all_doc_ids,
            prop_to_update=sdk_tracking_prop,
            number_updates=number_updates_per_doc_per_client
        )

        # Make sure to block on the result to catch any exceptions that may have been thrown
        # during execution of the future
        update_from_sg_task.result()
        update_from_sdk_task.result()

    # Issue a bulk_get to make sure all docs have auto imported
    docs_from_sg_bulk_get, errors = sg_client.get_bulk_docs(url=sg_url, db=sg_db, doc_ids=all_doc_ids, auth=seth_session)
    assert len(docs_from_sg_bulk_get) == number_docs_per_client * 2
    assert len(errors) == 0

    # Issue _changes
    docs_from_sg_bulk_get_formatted = [{"id": doc["_id"], "rev": doc["_rev"]} for doc in docs_from_sg_bulk_get]
    assert len(docs_from_sg_bulk_get_formatted) == number_docs_per_client * 2
    sg_client.verify_docs_in_changes(url=sg_url, db=sg_db, expected_docs=docs_from_sg_bulk_get_formatted, auth=seth_session)

    # Get all of the docs and verify that all updates we applied
    log_info('Verifying that all docs have the expected number of updates.')
    for doc_id in all_doc_ids:

        # Get doc from SDK
        doc_result = sdk_client.get(doc_id)
        doc_body = doc_result.content

        log_info('doc: {} -> {}:{}, {}:{}'.format(
            doc_id,
            sg_tracking_prop, doc_body[sg_tracking_prop],
            sdk_tracking_prop, doc_body[sdk_tracking_prop],
        ))

        assert doc_body['updates'] == number_updates_per_doc_per_client * 2
        assert doc_body[sg_tracking_prop] == number_updates_per_doc_per_client
        assert doc_body[sdk_tracking_prop] == number_updates_per_doc_per_client

        # Get doc from Sync Gateway
        doc = sg_client.get_doc(url=sg_url, db=sg_db, doc_id=doc_id, auth=seth_session)

        assert doc['updates'] == number_updates_per_doc_per_client * 2
        assert doc[sg_tracking_prop] == number_updates_per_doc_per_client
        assert doc[sdk_tracking_prop] == number_updates_per_doc_per_client

        # We cant be sure deterministically due to batched import from SDK updates
        # so make sure it has been update past initial write
        assert int(doc['_rev'].split('-')[0]) > 1
        assert len(doc['_revisions']['ids']) > 1

    # Try concurrent deletes from either side
    with ThreadPoolExecutor(max_workers=5) as tpe:

        sdk_delete_task = tpe.submit(
            delete_sdk_docs,
            client=sdk_client,
            docs_to_delete=all_doc_ids
        )

        sg_delete_task = tpe.submit(
            delete_sg_docs,
            client=sg_client,
            url=sg_url,
            db=sg_db,
            docs_to_delete=all_doc_ids,
            auth=seth_session
        )

        # Make sure to block on the result to catch any exceptions that may have been thrown
        # during execution of the future
        sdk_delete_task.result()
        sg_delete_task.result()

    assert len(all_doc_ids) == number_docs_per_client * 2

    # Verify all docs deleted from SG context
    verify_sg_deletes(client=sg_client, url=sg_url, db=sg_db, docs_to_verify_deleted=all_doc_ids, auth=seth_session)

    # Verify all docs deleted from SDK context
    verify_sdk_deletes(sdk_client, all_doc_ids)


@pytest.mark.syncgateway
@pytest.mark.xattrs
@pytest.mark.session
@pytest.mark.parametrize(
    'sg_conf_name, number_docs_per_client, number_updates_per_doc_per_client',
    [
        ('sync_gateway_default_functional_tests', 10, 10),
        ('sync_gateway_default_functional_tests', 100, 10),
        ('sync_gateway_default_functional_tests_no_port', 100, 10),
        pytest.param('sync_gateway_default_functional_tests_couchbase_protocol_withport_11210', 100, 10, marks=pytest.mark.oscertify),
        ('sync_gateway_default_functional_tests', 10, 100),
        ('sync_gateway_default_functional_tests_no_port', 10, 100),
        ('sync_gateway_default_functional_tests', 1, 1000)
    ]
)
def test_sg_feed_changed_with_xattrs_importEnabled(params_from_base_test_setup,
                                                   sg_conf_name,
                                                   number_docs_per_client,
                                                   number_updates_per_doc_per_client):
    """
    Scenario:
    - Start sync-gateway with Xattrs and import enabled
    - start listening to changes
    - Create docs via SDK
    - Verify docs via ChangesTracker with rev generation 1-
    - update docs via SDK
    - Verify docs via ChangesTracker with rev generation 2-
    - update SDK docs via SG
    - Verify docs via ChangesTracker with expected revision
    - Create docs via SG
    - Verify docs via ChangesTracker with expected revision
    - update docs via SG
    - Verify docs via ChangesTracker with expected revision
    - update SG docs via SDK
    - Verify docs via ChangesTracker with rev generation 3-
   """
    cluster_conf = params_from_base_test_setup['cluster_config']
    cluster_topology = params_from_base_test_setup['cluster_topology']
    mode = params_from_base_test_setup['mode']
    xattrs_enabled = params_from_base_test_setup['xattrs_enabled']
    ssl_enabled = params_from_base_test_setup["ssl_enabled"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    # Skip the test if ssl disabled as it cannot run without port using http protocol
    if ("sync_gateway_default_functional_tests_no_port" in sg_conf_name) and get_sg_version(cluster_conf) < "1.5.0":
        pytest.skip('couchbase/couchbases ports do not support for versions below 1.5')
    if "sync_gateway_default_functional_tests_no_port" in sg_conf_name and not ssl_enabled:
        pytest.skip('ssl disabled so cannot run without port')

    # Skip the test if ssl enabled as it cannot run using couchbase protocol
    # TODO : https://github.com/couchbaselabs/sync-gateway-accel/issues/227
    # Remove DI condiiton once above bug is fixed
    if "sync_gateway_default_functional_tests_couchbase_protocol_withport_11210" in sg_conf_name and (ssl_enabled or mode.lower() == "di"):
        pytest.skip('ssl enabled so cannot run with couchbase protocol')

    # This test should only run when using xattr meta storage
    if not xattrs_enabled:
        pytest.skip('XATTR tests require --xattrs flag')

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)
    sg_admin_url = cluster_topology['sync_gateways'][0]['admin']
    sg_url = cluster_topology['sync_gateways'][0]['public']
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None

    # bucket_name = 'data-bucket'
    buckets = get_buckets_from_sync_gateway_config(sg_conf, cluster_conf)
    bucket_name = buckets[0]
    cbs_url = cluster_topology['couchbase_servers'][0]
    sg_db = 'db'
    changesTracktimeout = 60

    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_config_path=sg_conf)

    sg_client = MobileRestClient()
    sg_client.create_user(url=sg_admin_url, db=sg_db, name='autosdkuser', password='pass', channels=['shared'], auth=auth)
    autosdkuser_session = sg_client.create_session(url=sg_admin_url, db=sg_db, name='autosdkuser', auth=auth)

    sg_client.create_user(url=sg_admin_url, db=sg_db, name='autosguser', password='pass', channels=['sg-shared'], auth=auth)
    autosguser_session = sg_client.create_session(url=sg_admin_url, db=sg_db, name='autosguser', auth=auth)

    log_info('Num docs per client: {}'.format(number_docs_per_client))
    log_info('Num updates per doc per client: {}'.format(number_updates_per_doc_per_client))

    log_info('sg_conf: {}'.format(sg_conf))
    log_info('sg_admin_url: {}'.format(sg_admin_url))
    log_info('sg_url: {}'.format(sg_url))

    sg_tracking_prop = 'sg_one_updates'
    sdk_tracking_prop = 'sdk_one_updates'

    # Start listening to changes feed
    changestrack = ChangesTracker(sg_url, sg_db, auth=autosdkuser_session)
    changestrack_sg = ChangesTracker(sg_url, sg_db, auth=autosguser_session)
    cbs_ip = host_for_url(cbs_url)

    # Connect to server via SDK
    if ssl_enabled and cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify&ipv6=allow".format(cbs_ip)
    elif ssl_enabled and not cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify".format(cbs_ip)
    elif not ssl_enabled and cluster.ipv6:
        connection_url = "couchbase://{}?ipv6=allow".format(cbs_ip)
    else:
        connection_url = 'couchbase://{}'.format(cbs_ip)
    sdk_client = get_cluster(connection_url, bucket_name)

    # Inject custom properties into doc template
    def update_props():
        return {
            'updates': 0,
            sg_tracking_prop: 0,
            sdk_tracking_prop: 0
        }

    # Create / add docs via sdk
    sdk_doc_bodies = document.create_docs(
        'doc_sdk_ids',
        number_docs_per_client,
        channels=['shared'],
        prop_generator=update_props,
        non_sgw=True
    )

    with ThreadPoolExecutor(max_workers=5) as crsdk_tpe:

        # Add docs via SDK
        log_info('Started adding {} docs via SDK ...'.format(number_docs_per_client))
        sdk_docs = {doc['id']: doc for doc in sdk_doc_bodies}
        doc_set_ids1 = [sdk_doc['id'] for sdk_doc in sdk_doc_bodies]
        sdk_docs_resp = []
        for k, v in sdk_docs.items():
            sdk_docs_resp.append(sdk_client.upsert(k, v))
        assert len(sdk_docs_resp) == number_docs_per_client
        assert len(doc_set_ids1) == number_docs_per_client
        log_info("Docs creation via SDK done")
        all_docs_via_sg_formatted = [
            {"id": doc, "rev": "1-"} for doc in doc_set_ids1]

        ct_task = crsdk_tpe.submit(changestrack.start())
        log_info("ct_task value {}".format(ct_task))

        wait_for_changes = crsdk_tpe.submit(
            changestrack.wait_until, all_docs_via_sg_formatted, rev_prefix_gen=True)

        if wait_for_changes.result():
            log_info("Found all docs ...")
        else:
            raise DocumentNotFoundException(
                "Could not find all changes in feed for adding docs via SDK before timeout!!")

    with ThreadPoolExecutor(max_workers=5) as upsdk_tpe:
        log_info("Updating docs via SDK...")

        # Update docs via SDK
        sdk_docs = sdk_client.get_multi(doc_set_ids1)
        assert len(list(sdk_docs.keys())) == number_docs_per_client
        for doc_id, val in list(sdk_docs.items()):
            doc_body = val.content
            doc_body["updated_by_sdk"] = True
            sdk_client.upsert(doc_id, doc_body)
        # Retry to get changes until expected changes appeared
        start = time.time()
        while True:
            if time.time() - start > changesTracktimeout:
                break
            try:
                ct_task = upsdk_tpe.submit(changestrack.start())
                break
            except ChangesError:
                continue
        all_docs_via_sg_formatted = [
            {"id": doc, "rev": "2-"} for doc in doc_set_ids1]

        wait_for_changes = upsdk_tpe.submit(
            changestrack.wait_until, all_docs_via_sg_formatted, rev_prefix_gen=True)

        if wait_for_changes.result():
            log_info("Found all docs after SDK update ...")
        else:
            raise DocumentNotFoundException(
                "Could not find all changes in feed for SDK updated SDK docs before timeout!!")

    # update docs by sync-gateway
    with ThreadPoolExecutor(max_workers=5) as upsdksg_tpe:
        log_info("Starting updating SDK docs by sync-gateway...")
        user_docs, errors = sg_client.get_bulk_docs(url=sg_url, db=sg_db, doc_ids=sdk_docs, auth=autosdkuser_session)
        assert len(errors) == 0

        # Update the 'updates' property
        for doc in user_docs:
            doc['updated_by_sg'] = True

        # Add the bulk docs via sync-gateway
        sg_docs_update_resp = sg_client.add_bulk_docs(url=sg_url, db=sg_db, docs=user_docs, auth=autosdkuser_session)
        # Retry to get changes until expected changes appeared
        start = time.time()
        while True:
            if time.time() - start > changesTracktimeout:
                break
            try:
                ct_task = upsdksg_tpe.submit(changestrack.start())
                break
            except ChangesError:
                continue
        wait_for_changes = upsdksg_tpe.submit(
            changestrack.wait_until, sg_docs_update_resp)

        if wait_for_changes.result():
            log_info("Stopping ...")
            log_info("Found all docs for update docs via sg ...")
            upsdksg_tpe.submit(changestrack.stop)
        else:
            upsdksg_tpe.submit(changestrack.stop)
            raise DocumentNotFoundException(
                "Could not find all changes in feed for SG updated SDK docs via sg before timeout!!")

    with ThreadPoolExecutor(max_workers=5) as crsg_tpe:
        log_info("Starting adding docs via sync-gateway...")

        # Create / add docs to sync gateway
        sg_docs = document.create_docs(
            'doc_sg_id',
            number_docs_per_client,
            channels=['sg-shared'],
            prop_generator=update_props
        )
        sg_docs_resp = sg_client.add_bulk_docs(
            url=sg_url,
            db=sg_db,
            docs=sg_docs,
            auth=autosguser_session
        )
        assert len(sg_docs_resp) == number_docs_per_client
        sg_docs = [doc['id'] for doc in sg_docs_resp]
        assert len(sg_docs) == number_docs_per_client
        # Retry to get changes until expected changes appeared
        start = time.time()
        while True:
            if time.time() - start > changesTracktimeout:
                break
            try:
                ct_task = crsg_tpe.submit(changestrack_sg.start())
                break
            except ChangesError:
                continue
        wait_for_changes = crsg_tpe.submit(
            changestrack_sg.wait_until, sg_docs_resp)

        if wait_for_changes.result():
            log_info("Found all docs ...")
        else:
            raise DocumentNotFoundException(
                "Could not find all changes in feed for sg created docs before timeout!!")

    # update docs by sync-gateway
    with ThreadPoolExecutor(max_workers=5) as upsg_tpe:
        log_info("Starting updating sg docs by sync-gateway...")
        user_docs, errors = sg_client.get_bulk_docs(url=sg_url, db=sg_db, doc_ids=sg_docs, auth=autosguser_session)
        assert len(errors) == 0

        # Update the 'updates' property
        for doc in user_docs:
            doc['updated_by_sg'] = "edits_1"

        # Add the docs via bulk_docs
        sg_docs_update_resp = sg_client.add_bulk_docs(url=sg_url, db=sg_db, docs=user_docs, auth=autosguser_session)
        # Retry to get changes until expected changes appeared
        start = time.time()
        while True:
            if time.time() - start > changesTracktimeout:
                break
            try:
                ct_task = upsg_tpe.submit(changestrack_sg.start())
                break
            except ChangesError:
                continue
        wait_for_changes = upsg_tpe.submit(
            changestrack_sg.wait_until, sg_docs_update_resp)

        if wait_for_changes.result():
            log_info("Found all sg docs for update docs via sg ...")
        else:
            raise DocumentNotFoundException(
                "Could not find all changes in feed for update sg docs via sg before timeout!!")

    # Update sg docs via SDK
    with ThreadPoolExecutor(max_workers=5) as upsgsdk_tpe:
        log_info("Updating sg docs via SDK...")

        sdk_docs = sdk_client.get_multi(sg_docs)
        assert len(list(sdk_docs.keys())) == number_docs_per_client
        for doc_id, val in list(sdk_docs.items()):
            doc_body = val.content
            doc_body["updated_by_sdk"] = True
            sdk_client.upsert(doc_id, doc_body)
        # Retry to get changes until expected changes appeared
        start = time.time()
        while True:
            if time.time() - start > changesTracktimeout:
                break
            try:
                ct_task = upsgsdk_tpe.submit(changestrack_sg.start())
                break
            except ChangesError:
                continue
        all_docs_via_sg_formatted = [
            {"id": doc, "rev": "3-"} for doc in sg_docs]

        wait_for_changes = upsgsdk_tpe.submit(changestrack_sg.wait_until, all_docs_via_sg_formatted, rev_prefix_gen=True)

        if wait_for_changes.result():
            log_info("Stopping sg changes track...")
            log_info("Found all sg docs after SDK update ...")
            upsgsdk_tpe.submit(changestrack_sg.stop)
        else:
            upsgsdk_tpe.submit(changestrack_sg.stop)
            raise DocumentNotFoundException(
                "Could not find all changes in feed for SDK updated sg docs before timeout!!")


def update_sg_docs(client, url, db, docs_to_update, prop_to_update, number_updates, auth=None):
    """
    1. Check if document has already been updated 'number_updates'
    1. Get random doc id from 'docs_to_update'
    2. Update the doc
    3. Check to see if it has been updated 'number_updates'
    4. If it has been updated the correct number of times, delete it from the the list
    """

    log_info("Client: {}".format(id(client)))

    # Store copy of list to avoid mutating 'docs_to_update'
    local_docs_to_update = list(docs_to_update)

    # 'docs_to_update' is a list of doc ids that the client should update a number of times
    # Once the doc has been updated the correct number of times, it will be removed from the list.
    # Loop until all docs have been removed
    while len(local_docs_to_update) > 0:
        random_doc_id = random.choice(local_docs_to_update)
        doc = client.get_doc(url=url, db=db, doc_id=random_doc_id, auth=auth)

        # Create property updater to modify custom property
        def property_updater(doc_body):
            doc_body[prop_to_update] += 1
            return doc_body

        # Remove doc from the list if the doc has been updated enough times
        if doc[prop_to_update] == number_updates:
            local_docs_to_update.remove(doc["_id"])
        else:
            # Update the doc
            try:
                log_info('Updating: {} from SG'.format(random_doc_id))
                client.update_doc(url=url, db=db, doc_id=random_doc_id, property_updater=property_updater, auth=auth)
            except HTTPError as e:
                # This is possible if hitting a conflict. Check that it is. If it is not, we want to raise
                if not is_conflict(e):
                    raise
                else:
                    log_info('Hit a conflict! Will retry later ...')

        # SDK and sync gateway do not operate at the same speed.
        # This will help normalize the rate
        time.sleep(SG_OP_SLEEP)


def is_conflict(httperror):
    if httperror.response.status_code == 409 \
            and str(httperror).startswith('409 Client Error: Conflict for url:'):
        return True
    else:
        return False


def update_sdk_docs(client, docs_to_update, prop_to_update, number_updates):
    """ This will update a set of docs (docs_to_update)
    by updating a property (prop_to_update) using CAS safe writes.
    It will continue to update the set of docs until all docs have
    been updated a number of times (number_updates).
    """

    log_info("Client: {}".format(id(client)))

    # Store copy of list to avoid mutating 'docs_to_update'
    local_docs_to_update = list(docs_to_update)

    while len(local_docs_to_update) > 0:
        random_doc_id = random.choice(local_docs_to_update)
        log_info(random_doc_id)

        doc = client.get(random_doc_id)
        doc_body = doc.content

        # Make sure not meta is seen
        assert '_sync' not in doc_body

        if doc_body[prop_to_update] == number_updates:
            local_docs_to_update.remove(random_doc_id)
        else:
            try:
                # Do a CAS safe write. It is possible that the document is updated
                # by Sync Gateway between the client.get and the client.upsert.
                # If this happens, catch the CAS error and retry
                doc_body[prop_to_update] += 1
                doc_body['updates'] += 1
                log_info('Updating: {} from SDK'.format(random_doc_id))
                cur_cas = doc.cas
                client.upsert(random_doc_id, doc_body, cas=cur_cas)
            except CouchbaseException:
                log_info('CAS mismatch from SDK. Will retry ...')

        # SDK and sync gateway do not operate at the same speed.
        # This will help normalize the rate
        time.sleep(SDK_OP_SLEEP)


def delete_sg_docs(client, url, db, docs_to_delete, auth):
    """ This will attempt to delete a document via Sync Gateway. This method is meant to be
    run concurrently with delete_sdk_docs so the deletions have to handle external deletions
    as well.
    """

    deleted_count = 0

    # Create a copy of all doc ids
    docs_to_remove = list(docs_to_delete)
    while len(docs_to_remove) > 0:
        random_doc_id = random.choice(docs_to_remove)
        log_info('Attempting to delete from SG: {}'.format(random_doc_id))
        try:
            doc_to_delete = client.get_doc(url=url, db=db, doc_id=random_doc_id, auth=auth)
            deleted_doc = client.delete_doc(url=url, db=db, doc_id=random_doc_id, rev=doc_to_delete['_rev'], auth=auth)
            docs_to_remove.remove(deleted_doc['id'])
            deleted_count += 1
        except HTTPError as he:
            if ((he.response.status_code == 403 and str(he).startswith('403 Client Error: Forbidden for url:')) or
                (he.response.status_code == 409 and str(he).startswith('409 Client Error: Conflict for url:')) or
                    (he.response.status_code == 404 and str(he).startswith('404 Client Error: Not Found for url:'))):
                # Doc may have been deleted by the SDK and GET fails for SG
                # Conflict for url can happen in the following scenario:
                # During concurrent deletes from SG and SDK,
                #  1. SG GETs doc 'a' with rev '2'
                #  2. SDK deletes doc 'a' with rev '2' before
                #  3. SG tries to DELETE doc 'a' with rev '2' and GET a conflict
                log_info('Could not find doc, must have been deleted by SDK. Retrying ...')
                docs_to_remove.remove(random_doc_id)
            else:
                raise he

        # SDK and sync gateway do not operate at the same speed.
        # This will help normalize the rate
        time.sleep(SG_OP_SLEEP)

    # If the scenario is the one doc per client, it is possible that the SDK may delete both docs
    # before Sync Gateway has a chance to delete one. Only assert when we have enought docs to
    # ensure both sides get a chances to delete
    if len(docs_to_delete) > 2:
        assert deleted_count > 0


def delete_sdk_docs(client, docs_to_delete):
    """ This will attempt to delete a document via Couchbase Server Python SDK. This method is meant to be
    run concurrently with delete_sg_docs so the deletions have to handle external deletions by Sync Gateway.
    """

    deleted_count = 0

    # Create a copy of all doc ids
    docs_to_remove = list(docs_to_delete)
    while len(docs_to_remove) > 0:
        random_doc_id = random.choice(docs_to_remove)
        log_info('Attempting to delete from SDK: {}'.format(random_doc_id))
        try:
            doc = client.remove(random_doc_id)
            log_info(doc, "docdata")
            docs_to_remove.remove(random_doc_id)
            deleted_count += 1
        except DocumentNotFoundException:
            # Doc may have been deleted by sync gateway
            log_info('Could not find doc, must have been deleted by SG. Retrying ...')
            docs_to_remove.remove(random_doc_id)

        # SDK and sync gateway do not operate at the same speed.
        # This will help normalize the rate
        time.sleep(SDK_OP_SLEEP)

    # If the scenario is the one doc per client, it is possible that the SDK may delete both docs
    # before Sync Gateway has a chance to delete one. Only assert when we have enought docs to
    # ensure both sides get a chances to delete
    if len(docs_to_delete) > 2:
        assert deleted_count > 0


def verify_sg_deletes(client, url, db, docs_to_verify_deleted, auth):
    """ Verify that documents have been deleted via Sync Gateway GET's.
    - Verify the expected result is returned via GET doc
    - Verify the expected result is returned via GET _bulk_get
    """

    docs_to_verify_scratchpad = list(docs_to_verify_deleted)

    # Verify deletes via individual GETs
    for doc_id in docs_to_verify_deleted:
        he = None
        with pytest.raises(HTTPError) as he:
            client.get_doc(url=url, db=db, doc_id=doc_id, auth=auth)

        assert he is not None
        log_info(str(he.value))

        assert str(he.value).startswith('404 Client Error: Not Found for url:') or \
            str(he.value).startswith('403 Client Error: Forbidden for url:')

        # Parse out the doc id
        # sg_0?conflicts=true&revs=true
        parts = str(he.value).split('/')[-1]
        doc_id_from_parts = parts.split('?')[0]

        # Remove the doc id from the list
        docs_to_verify_scratchpad.remove(doc_id_from_parts)

    assert len(docs_to_verify_scratchpad) == 0

    # Create a new verify list
    docs_to_verify_scratchpad = list(docs_to_verify_deleted)

    # Verify deletes via bulk_get
    try_get_bulk_docs, errors = client.get_bulk_docs(url=url, db=db, doc_ids=docs_to_verify_deleted, auth=auth, validate=False)
    assert len(try_get_bulk_docs) == 0
    assert len(errors) == len(docs_to_verify_deleted)

    # Verify each deletion
    for err in errors:
        status = err['status']
        assert status in [403, 404]
        if status == 403:
            assert err['error'] == 'forbidden'
            assert err['reason'] == 'forbidden'
        else:
            assert err['error'] == 'not_found'
            assert err['reason'] == 'deleted'

        assert err['id'] in docs_to_verify_deleted
        # Cross off the doc_id
        docs_to_verify_scratchpad.remove(err['id'])

    # Verify that all docs have been removed
    assert len(docs_to_verify_scratchpad) == 0


def verify_sdk_deletes(sdk_client, docs_ids_to_verify_deleted):
    """ Verifies that all doc ids have been deleted from the SDK """

    docs_to_verify_scratchpad = list(docs_ids_to_verify_deleted)

    for doc_id in docs_ids_to_verify_deleted:
        nfe = None
        with pytest.raises(DocumentNotFoundException) as nfe:
            sdk_client.get(doc_id)
        assert nfe is not None
        assert 'DOCUMENT_NOT_FOUND' in str(nfe)
        docs_to_verify_scratchpad.remove(doc_id)

    # Verify that all docs have been removed
    assert len(docs_to_verify_scratchpad) == 0


def verify_sg_xattrs(mode, sg_client, sg_url, sg_db, doc_id, expected_number_of_revs, expected_number_of_channels, deleted_docs=False, auth=None):
    """ Verify expected values for xattr sync meta data via Sync Gateway _raw """

    # Get Sync Gateway sync meta
    raw_doc = sg_client.get_raw_doc(sg_url, db=sg_db, doc_id=doc_id, auth=auth)
    sg_sync_meta = raw_doc['_sync']

    log_info('Verifying XATTR (expected num revs: {}, expected num channels: {})'.format(
        expected_number_of_revs,
        expected_number_of_channels,
    ))

    # Distributed index mode uses server's internal vbucket sequence
    # It does not expose this to the '_sync' meta
    if mode != 'di':
        assert isinstance(sg_sync_meta['sequence'], int)
        assert isinstance(sg_sync_meta['recent_sequences'], list)
        assert len(sg_sync_meta['recent_sequences']) == expected_number_of_revs

    assert isinstance(sg_sync_meta['cas'], str)
    assert sg_sync_meta['rev'].startswith('{}-'.format(expected_number_of_revs))
    assert isinstance(sg_sync_meta['channels'], dict)
    assert len(sg_sync_meta['channels']) == expected_number_of_channels
    assert isinstance(sg_sync_meta['time_saved'], str)
    assert isinstance(sg_sync_meta['history']['channels'], list)
    assert len(sg_sync_meta['history']['channels']) == expected_number_of_revs
    assert isinstance(sg_sync_meta['history']['revs'], list)
    assert len(sg_sync_meta['history']['revs']) == expected_number_of_revs
    assert isinstance(sg_sync_meta['history']['parents'], list)


def verify_no_sg_xattrs(sg_client, sg_url, sg_db, doc_id):
    """ Verify that _sync no longer exists in the the xattrs.
    This should be the case once a document is purged. """

    # Try to get Sync Gateway sync meta
    he = None
    with pytest.raises(HTTPError) as he:
        sg_client.get_raw_doc(sg_url, db=sg_db, doc_id=doc_id)
    assert he is not None
    assert 'HTTPError: 404 Client Error: Not Found for url:' in str(he)
    log_info(he.value)


def verify_doc_ids_in_sg_bulk_response(response, expected_number_docs, expected_ids):
    """ Verify 'expected_ids' are present in Sync Gateway _build_get request """

    log_info('Verifing SG bulk_get response has {} docs with expected ids ...'.format(expected_number_docs))

    expected_ids_scratch_pad = list(expected_ids)
    assert len(expected_ids_scratch_pad) == expected_number_docs
    assert len(response) == expected_number_docs

    # Cross off all the doc ids seen in the response from the scratch pad
    for doc in response:
        expected_ids_scratch_pad.remove(doc["_id"])

    # Make sure all doc ids have been found
    assert len(expected_ids_scratch_pad) == 0


def verify_doc_ids_in_sg_all_docs_response(response, expected_number_docs, expected_ids):
    """ Verify 'expected_ids' are present in Sync Gateway _all_docs request """

    log_info('Verifing SG all_docs response has {} docs with expected ids ...'.format(expected_number_docs))

    expected_ids_scratch_pad = list(expected_ids)
    assert len(expected_ids_scratch_pad) == expected_number_docs
    assert len(response['rows']) == expected_number_docs

    # Cross off all the doc ids seen in the response from the scratch pad
    for doc in response['rows']:
        expected_ids_scratch_pad.remove(doc['id'])

    # Make sure all doc ids have been found
    assert len(expected_ids_scratch_pad) == 0


def verify_doc_ids_in_sdk_get_multi(response, expected_number_docs, expected_ids):
    """ Verify 'expected_ids' are present in Python SDK get_multi() call """

    log_info('Verifing SDK get_multi response has {} docs with expected ids ...'.format(expected_number_docs))

    expected_ids_scratch_pad = list(expected_ids)
    assert len(expected_ids_scratch_pad) == expected_number_docs
    assert len(response) == expected_number_docs

    # Cross off all the doc ids seen in the response from the scratch pad
    for doc_id, value in list(response.items()):
        assert '_sync' not in value.content
        expected_ids_scratch_pad.remove(doc_id)

    # Make sure all doc ids have been found
    assert len(expected_ids_scratch_pad) == 0


@pytest.mark.syncgateway
@pytest.mark.xattrs
@pytest.mark.session
@pytest.mark.parametrize(
    'sg_conf_name, number_docs_per_client, number_updates_per_doc_per_client',
    [
        ('sync_gateway_default_functional_tests', 10, 10),
        ('sync_gateway_default_functional_tests', 100, 10),
        pytest.param('sync_gateway_default_functional_tests_no_port', 100, 10, marks=pytest.mark.oscertify),
        ('sync_gateway_default_functional_tests_couchbase_protocol_withport_11210', 100, 10),
        ('sync_gateway_default_functional_tests_couchbase_protocol_withport_11210', 100, 10),
        ('sync_gateway_default_functional_tests', 10, 100),
        ('sync_gateway_default_functional_tests_no_port', 10, 100),
        ('sync_gateway_default_functional_tests', 1, 1000)
    ]
)
def test_sg_sdk_interop_shared_updates_from_sg(params_from_base_test_setup,
                                               sg_conf_name,
                                               number_docs_per_client,
                                               number_updates_per_doc_per_client):
    """
    Scenario:
    - Create docs via SG and get the revision number 1-rev
    - Update docs via SDK and get the revision number 2-rev
    - Update docs via SG with new_edits=false by giving parent revision 1-rev
        and get the revision number 2-rev1
    - update docs via SDK again and get the revision number 3-rev
    - Verify with _all_changes by enabling include docs and verify 2 branched revisions appear in changes feed
    - Verify no errors occur while updating docs via SG
    - Delete docs via SDK
    - Delete docs via SG
    - Verify no errors while deletion
    - Verify changes feed that branched revision are removed
    - Verify changes feed that keys "deleted" is true and keys "removed"
    """

    cluster_conf = params_from_base_test_setup['cluster_config']
    cluster_topology = params_from_base_test_setup['cluster_topology']
    mode = params_from_base_test_setup['mode']
    xattrs_enabled = params_from_base_test_setup['xattrs_enabled']
    no_conflicts_enabled = params_from_base_test_setup["no_conflicts_enabled"]
    ssl_enabled = params_from_base_test_setup["ssl_enabled"]
    log_info("sg version is this -------{}".format(get_sg_version(cluster_conf)))
    # Skip the test if ssl disabled as it cannot run without port using http protocol
    if ("sync_gateway_default_functional_tests_no_port" in sg_conf_name) and get_sg_version(cluster_conf) < "1.5.0":
        pytest.skip('couchbase/couchbases ports do not support for versions below 1.5')
    if "sync_gateway_default_functional_tests_no_port" in sg_conf_name and not ssl_enabled:
        pytest.skip('ssl disabled so cannot run without port')

    # Skip the test if ssl enabled as it cannot run using couchbase protocol
    # TODO : https://github.com/couchbaselabs/sync-gateway-accel/issues/227
    # Remove DI condiiton once above bug is fixed
    if "sync_gateway_default_functional_tests_couchbase_protocol_withport_11210" in sg_conf_name and (ssl_enabled or mode.lower() == "di"):
        pytest.skip('ssl enabled so cannot run with couchbase protocol')

    # This test should only run when using xattr meta storage
    if not xattrs_enabled:
        pytest.skip('XATTR tests require --xattrs flag')
    if no_conflicts_enabled:
        pytest.skip('--no-conflicts is enabled, this test needs to create conflicts, so skipping the test')

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)
    sg_admin_url = cluster_topology['sync_gateways'][0]['admin']
    sg_url = cluster_topology['sync_gateways'][0]['public']
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    # bucket_name = 'data-bucket'
    buckets = get_buckets_from_sync_gateway_config(sg_conf, cluster_conf)
    bucket_name = buckets[0]
    cbs_url = cluster_topology['couchbase_servers'][0]
    sg_db = 'db'
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None

    log_info('Num docs per client: {}'.format(number_docs_per_client))
    log_info('Num updates per doc per client: {}'.format(number_updates_per_doc_per_client))

    log_info('sg_conf: {}'.format(sg_conf))
    log_info('sg_admin_url: {}'.format(sg_admin_url))
    log_info('sg_url: {}'.format(sg_url))

    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_config_path=sg_conf)

    sg_tracking_prop = 'sg_one_updates'
    sdk_tracking_prop = 'sdk_one_updates'

    # Create sg user
    sg_client = MobileRestClient()
    sg_client.create_user(url=sg_admin_url, db=sg_db, name='autotest', password='pass', channels=['shared'], auth=auth)
    autouser_session = sg_client.create_session(url=sg_admin_url, db=sg_db, name='autotest', auth=auth)

    # Connect to server via SDK
    cbs_ip = host_for_url(cbs_url)
    if ssl_enabled and cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify&ipv6=allow".format(cbs_ip)
    elif ssl_enabled and not cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify".format(cbs_ip)
    elif not ssl_enabled and cluster.ipv6:
        connection_url = "couchbase://{}?ipv6=allow".format(cbs_ip)
    else:
        connection_url = 'couchbase://{}'.format(cbs_ip)
    sdk_client = get_cluster(connection_url, bucket_name)

    # Inject custom properties into doc template
    def update_props():
        return {
            'updates': 0,
            sg_tracking_prop: 0,
            sdk_tracking_prop: 0
        }

    # Create / add docs to sync gateway
    sg_docs = document.create_docs(
        'sg_doc',
        number_docs_per_client,
        channels=['shared'],
        prop_generator=update_props
    )
    sg_docs_resp = sg_client.add_bulk_docs(
        url=sg_url,
        db=sg_db,
        docs=sg_docs,
        auth=autouser_session
    )

    sg_doc_ids = [doc['_id'] for doc in sg_docs]
    assert len(sg_docs_resp) == number_docs_per_client

    sg_create_docs, errors = sg_client.get_bulk_docs(url=sg_url, db=sg_db, doc_ids=sg_doc_ids,
                                                     auth=autouser_session)
    assert len(errors) == 0
    sg_create_doc = sg_create_docs[0]["_rev"]
    assert(sg_create_doc.startswith("1-"))
    log_info("Sg created  doc revision :{}".format(sg_create_doc))

    # Update docs via SDK
    sdk_docs = sdk_client.get_multi(sg_doc_ids)
    assert len(list(sdk_docs.keys())) == number_docs_per_client
    for doc_id, val in sdk_docs.items():
        doc_body = val.content
        doc_body["updated_by_sdk"] = True
        sdk_client.upsert(doc_id, doc_body)

    sdk_first_update_docs, errors = sg_client.get_bulk_docs(url=sg_url, db=sg_db, doc_ids=sg_doc_ids,
                                                            auth=autouser_session)
    assert len(errors) == 0
    sdk_first_update_doc = sdk_first_update_docs[0]["_rev"]
    log_info("Sdk first update doc {}".format(sdk_first_update_doc))
    assert(sdk_first_update_doc.startswith("2-"))
    # Update the 'updates' property
    for doc in sg_create_docs:
        # update  docs via sync-gateway
        sg_client.add_conflict(
            url=sg_url,
            db=sg_db,
            doc_id=doc["_id"],
            parent_revisions=doc["_rev"],
            new_revision="2-bar",
            auth=autouser_session
        )

    sg_update_docs, errors = sg_client.get_bulk_docs(url=sg_url, db=sg_db, doc_ids=sg_doc_ids,
                                                     auth=autouser_session)
    assert len(errors) == 0
    sg_update_doc = sg_update_docs[0]["_rev"]
    log_info("sg update doc revision is : {}".format(sg_update_doc))
    assert(sg_update_doc.startswith("2-"))
    # Update docs via SDK
    sdk_docs = sdk_client.get_multi(sg_doc_ids)
    assert len(list(sdk_docs.keys())) == number_docs_per_client
    for doc_id, val in list(sdk_docs.items()):
        doc_body = val.content
        doc_body["updated_by_sdk2"] = True
        sdk_client.upsert(doc_id, doc_body)

    sdk_update_docs2, errors = sg_client.get_bulk_docs(url=sg_url, db=sg_db, doc_ids=sg_doc_ids,
                                                       auth=autouser_session)
    assert len(errors) == 0
    sdk_update_doc2 = sdk_update_docs2[0]["_rev"]
    log_info("sdk 2nd update doc revision is : {}".format(sdk_update_doc2))
    assert(sdk_update_doc2.startswith("3-"))
    time.sleep(2)  # Need some delay to have _changes to update with latest branched revisions
    # Get branched revision tree via _changes with include docs
    docs_changes = sg_client.get_changes_style_all_docs(url=sg_url, db=sg_db, auth=autouser_session, include_docs=True)
    doc_changes_in_changes = [change["changes"] for change in docs_changes["results"]]
    # Iterate through all docs and verify branched revisions appear in changes feed, verify previous revisions
    # which created before branched revisions does not show up in changes feed
    for docs in doc_changes_in_changes[1:]:  # skip first item in list as first item has user information, but not doc information
        revs = [doc['rev'] for doc in docs]
        if sdk_first_update_doc in revs:
            assert True
        else:
            log_info("conflict revision does not exist {}".format(revs))
            assert False
        if sg_create_doc not in revs and sg_update_doc not in revs:
            assert True
        else:
            log_info("Non conflict revision exist {} ".format(revs))
            assert False

    for doc in sdk_update_docs2:
        change_for_doc = sg_client.get_changes(url=sg_url, db=sg_db, since=0, auth=autouser_session, feed="normal", filter_type="_doc_ids", filter_doc_ids=[doc["_id"]])
        assert doc["_rev"] in change_for_doc["results"][0]["changes"][0]["rev"], "current revision does not exist in changes"

    # Do SDK deleted and SG delete after branched revision created and check changes feed removed branched revisions
    sdk_client.remove_multi(sg_doc_ids)
    time.sleep(1)  # Need some delay to have _changes to update with latest branched revisions
    sdk_deleted_docs, errors = sg_client.get_bulk_docs(url=sg_url, db=sg_db, doc_ids=sg_doc_ids,
                                                       auth=autouser_session)
    assert len(errors) == 0
    sdk_deleted_doc = sdk_deleted_docs[0]["_rev"]
    log_info("sdk deleted doc revision :{}".format(sdk_deleted_doc))
    assert(sdk_deleted_doc.startswith("2-"))
    sg_client.delete_docs(url=sg_url, db=sg_db, docs=sg_docs_resp, auth=autouser_session)
    time.sleep(1)  # Need some delay to have _changes to update with latest branched revisions
    docs_changes1 = sg_client.get_changes_style_all_docs(url=sg_url, db=sg_db, auth=autouser_session, include_docs=True)
    doc_changes_in_changes = [change["changes"] for change in docs_changes1["results"]]
    deleted_doc_revisions = [change["doc"]["_deleted"] for change in docs_changes1["results"][1:]]
    removedchannel_doc_revisions = [change["removed"] for change in docs_changes1["results"][1:]]
    assert len(deleted_doc_revisions) == number_docs_per_client
    assert len(removedchannel_doc_revisions) == number_docs_per_client

    # Verify in changes feed that new branched revisions are created after deletion of branced revisions which created
    # by sg update and sdk update.
    for docs in doc_changes_in_changes[1:]:
        revs = [doc['rev'] for doc in docs]
        assert len(revs) == 2
        if sdk_first_update_doc not in revs and sdk_update_doc2 not in revs and sg_create_doc not in revs and sg_update_doc not in revs:
            assert True
        else:
            log_info(
                "Deleted branched revisions still appear here {}".format(revs))
            assert False


@pytest.mark.syncgateway
@pytest.mark.xattrs
@pytest.mark.oscertify
@pytest.mark.parametrize('sg_conf_name', [
    'sync_gateway_default_functional_tests'
])
def test_purge_and_view_compaction(params_from_base_test_setup, sg_conf_name):
    """
    Scenario:
    - Generate some tombstones doc
    - Verify meta data still exists by verifygin sg xattrs using _raw sync gateway API
    - Execute a view query to see the tombstones -> http GET localhost:4985/default/_view/channels
        -> should see tombstone doc
    - Sleep for 5 mins to verify tomstone doc is available in view query and meta data
    - Trigger purge API to force the doc to purge
    - Verify meta data does not exists by verifying sg xattrs using _raw sync gateway API
    - Execute a view query to see the tombstones -> http GET localhost:4985/default/_view/channels
        -> should see tombstone doc after the purge
    - Trigger _compact API to compact the tombstone doc
    - Verify tomstones are not seen in view query
    """

    sg_db = 'db'
    cluster_conf = params_from_base_test_setup['cluster_config']
    cluster_topology = params_from_base_test_setup['cluster_topology']
    mode = params_from_base_test_setup['mode']
    xattrs_enabled = params_from_base_test_setup['xattrs_enabled']
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    # This test should only run when using xattr meta storage
    if not xattrs_enabled or mode == "di":
        pytest.skip('This test is di mode or xattrs not enabled')

    if get_sg_version(cluster_conf) > "2.0.0" and not get_sg_use_views(cluster_conf):
        pytest.skip("This test uses view queries")
    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)
    sg_admin_url = cluster_topology['sync_gateways'][0]['admin']
    sg_url = cluster_topology['sync_gateways'][0]['public']
    cbs_url = cluster_topology['couchbase_servers'][0]
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None

    log_info('sg_conf: {}'.format(sg_conf))
    log_info('sg_admin_url: {}'.format(sg_admin_url))
    log_info('sg_url: {}'.format(sg_url))
    log_info('cbs_url: {}'.format(cbs_url))

    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_config_path=sg_conf)
    # Create clients
    sg_client = MobileRestClient()
    channels = ['tombstone_test']

    # Create user / session
    auto_user_info = UserInfo(name='autotest', password='pass', channels=channels, roles=[])
    sg_client.create_user(
        url=sg_admin_url,
        db=sg_db,
        name=auto_user_info.name,
        password=auto_user_info.password,
        channels=auto_user_info.channels,
        auth=auth
    )

    test_auth_session = sg_client.create_session(
        url=sg_admin_url,
        db=sg_db,
        name=auto_user_info.name,
        auth=auth
    )

    def update_prop():
        return {
            'updates': 0,
            'tombstone': 'true',
        }

    doc_id = 'tombstone_test_sg_doc'
    doc_body = document.create_doc(doc_id=doc_id, channels=['tombstone_test'], prop_generator=update_prop)
    sg_client.add_doc(url=sg_url, db=sg_db, doc=doc_body, auth=test_auth_session)
    doc = sg_client.get_doc(url=sg_url, db=sg_db, doc_id=doc_id, auth=test_auth_session)
    sg_client.delete_doc(url=sg_url, db=sg_db, doc_id=doc_id, rev=doc['_rev'], auth=test_auth_session)
    number_revs_per_doc = 1
    verify_sg_xattrs(
        mode,
        sg_client,
        sg_url=sg_admin_url,
        sg_db=sg_db,
        doc_id=doc_id,
        expected_number_of_revs=number_revs_per_doc + 1,
        expected_number_of_channels=len(channels),
        deleted_docs=True,
        auth=auth
    )
    start = time.time()
    timeout = 10  # timeout for view query in channels due to race condition after compacting the docs
    while True:
        channel_view_query = sg_client.view_query_through_channels(url=sg_admin_url, db=sg_db, auth=auth)
        channel_view_query_string = json.dumps(channel_view_query)
        if(doc_id in channel_view_query_string or time.time() - start > timeout):
            break
    assert doc_id in channel_view_query_string, "doc id not exists in view query"
    time.sleep(300)  # wait for 5 mins and see meta is still available as it is not purged yet
    verify_sg_xattrs(
        mode,
        sg_client,
        sg_url=sg_admin_url,
        sg_db=sg_db,
        doc_id=doc_id,
        expected_number_of_revs=number_revs_per_doc + 1,
        expected_number_of_channels=len(channels),
        deleted_docs=True,
        auth=auth
    )
    channel_view_query_string = sg_client.view_query_through_channels(url=sg_admin_url, db=sg_db, auth=auth)
    channel_view_query_string = json.dumps(channel_view_query)
    assert doc_id in channel_view_query_string, "doc id not exists in view query"
    docs = []
    docs.append(doc)
    purged_doc = sg_client.purge_docs(url=sg_admin_url, db=sg_db, docs=docs, auth=auth)
    log_info("Purged doc is {}".format(purged_doc))
    verify_no_sg_xattrs(
        sg_client=sg_client,
        sg_url=sg_url,
        sg_db=sg_db,
        doc_id=doc_id
    )
    channel_view_query = sg_client.view_query_through_channels(url=sg_admin_url, db=sg_db, auth=auth)
    channel_view_query_string = json.dumps(channel_view_query)
    assert doc_id in channel_view_query_string, "doc id not exists in view query"
    sg_client.compact_database(url=sg_admin_url, db=sg_db, auth=auth)
    start = time.time()
    timeout = 10  # timeout for view query in channels due to race condition after compacting the docs
    while True:
        channel_view_query = sg_client.view_query_through_channels(url=sg_admin_url, db=sg_db, auth=auth)
        channel_view_query_string = json.dumps(channel_view_query)
        if(doc_id not in channel_view_query_string or time.time() - start > timeout):
            break
    assert doc_id not in channel_view_query_string, "doc id exists in chanel view query after compaction"


@pytest.mark.syncgateway
@pytest.mark.xattrs
@pytest.mark.session
@pytest.mark.oscertify
@pytest.mark.parametrize(
    'sg_conf_name, number_docs_per_client, number_updates_per_doc_per_client',
    [
        ('custom_sync/sync_gateway_custom_sync_require_roles', 10, 10),
    ]
)
def test_stats_logging_import_count(params_from_base_test_setup,
                                    sg_conf_name,
                                    number_docs_per_client,
                                    number_updates_per_doc_per_client):
    """
    Scenario:
      1. Have sync-gateway config to throw require role if user without try to create doc without role,
         otherwise throw forbidden
      2. create user without role
      3. Create docs via SDK with the channels mentioned in the sg config
      4. Create few more docs via SDK with the channels not mentioend in sg config
      5. Verify import_count, import_error_count stats incremented
      6. Create docs via sg with the user which can throw require role
      7. Verify stats for num_access_errors has incremented
   """
    cluster_conf = params_from_base_test_setup['cluster_config']
    cluster_topology = params_from_base_test_setup['cluster_topology']
    mode = params_from_base_test_setup['mode']
    xattrs_enabled = params_from_base_test_setup['xattrs_enabled']
    ssl_enabled = params_from_base_test_setup["ssl_enabled"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    # This test should only run when using xattr meta storage
    if not xattrs_enabled:
        pytest.skip('XATTR tests require --xattrs flag')

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)
    sg_admin_url = cluster_topology['sync_gateways'][0]['admin']
    sg_url = cluster_topology['sync_gateways'][0]['public']
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None

    # bucket_name = 'data-bucket'
    buckets = get_buckets_from_sync_gateway_config(sg_conf, cluster_conf)
    bucket_name = buckets[0]
    cbs_url = cluster_topology['couchbase_servers'][0]
    sg_db = 'db'

    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_config_path=sg_conf)

    sg_client = MobileRestClient()

    sg_client.create_user(url=sg_admin_url, db=sg_db, name='autosdkuser', password='pass', channels=['KMOW'], auth=auth)
    autosdkuser_session = sg_client.create_session(url=sg_admin_url, db=sg_db, name='autosdkuser', auth=auth)

    # TODO : will remove once dev fix the bug, need to verify whether it is required or not
    # sg_client.create_user(url=sg_admin_url, db=sg_db, name='autosguser', password='pass', channels=['sg-shared'])
    # autosguser_session = sg_client.create_session(url=sg_admin_url, db=sg_db, name='autosguser', password='pass')

    log_info('sg_conf: {}'.format(sg_conf))
    log_info('sg_admin_url: {}'.format(sg_admin_url))
    log_info('sg_url: {}'.format(sg_url))

    cbs_ip = host_for_url(cbs_url)

    # Connect to server via SDK
    if ssl_enabled and cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify&ipv6=allow".format(cbs_ip)
    elif ssl_enabled and not cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify".format(cbs_ip)
    elif not ssl_enabled and cluster.ipv6:
        connection_url = "couchbase://{}?ipv6=allow".format(cbs_ip)
    else:
        connection_url = 'couchbase://{}'.format(cbs_ip)
    sdk_client = get_cluster(connection_url, bucket_name)

    # Create / add docs via sdk with
    sdk_doc_bodies = document.create_docs(
        'doc_sdk_ids',
        number_docs_per_client,
        channels=['KMOW'],
        non_sgw=True)

    # Add docs via SDK
    log_info('Started adding {} docs via SDK as first set...'.format(number_docs_per_client))
    sdk_docs = {doc['id']: doc for doc in sdk_doc_bodies}
    doc_set_ids1 = [sdk_doc['id'] for sdk_doc in sdk_doc_bodies]
    sdk_docs_resp = []
    for k, v in sdk_docs.items():
        sdk_docs_resp.append(sdk_client.upsert(k, v))
    assert len(sdk_docs_resp) == number_docs_per_client
    assert len(doc_set_ids1) == number_docs_per_client
    log_info("Docs creation via SDK done")

    # Create / add docs via sdk with  -> 2nd set
    sdk_doc_bodies_2 = document.create_docs(
        'doc_sdk_ids-2',
        number_docs_per_client,
        channels=['sg-shared'],
        non_sgw=True)

    # Add docs via SDK
    log_info('Started adding {} docs via SDK as second set...'.format(number_docs_per_client))
    sdk_docs_2 = {doc['id']: doc for doc in sdk_doc_bodies_2}
    doc_set_ids2 = [sdk_doc['id'] for sdk_doc in sdk_doc_bodies_2]
    sdk_docs_resp = []
    for k, v in sdk_docs_2.items():
        sdk_docs_resp.append(sdk_client.upsert(k, v))
    assert len(sdk_docs_resp) == number_docs_per_client
    assert len(doc_set_ids2) == number_docs_per_client
    log_info("Docs creation 2nd set via SDK done")

    # Write docs via sg
    doc_body = document.create_doc("new_doc_id", channels=['KMOW'])
    try:
        sg_client.add_doc(url=sg_url, db=sg_db, doc=doc_body, auth=autosdkuser_session)
    except HTTPError:
        log_info("caught the expected exception")

    time.sleep(1)
    if sync_gateway_version >= "2.5.0":
        expvars = sg_client.get_expvars(sg_admin_url, auth=auth)
        assert expvars["syncgateway"]["per_db"][sg_db]["shared_bucket_import"]["import_count"] == 10, "import_count is not incremented"
        assert expvars["syncgateway"]["per_db"][sg_db]["shared_bucket_import"]["import_error_count"] == 10, "import_error count is not incremented"
        assert expvars["syncgateway"]["per_db"][sg_db]["security"]["num_access_errors"] == 1, "num_access_errors is not incremented"


@pytest.mark.syncgateway
@pytest.mark.xattrs
def test_non_mobile_revision(params_from_base_test_setup):
    """
    Scenario:
        1.   Sync Gateway running with shared bucket access enabled, server version 6.6.0 or higher
        2.   Document A is a Couchbase Server tombstone, but is not a mobile tombstone (i.e. doesn't have a _sync xattr)
            - You can get a document into this initial state in various ways, one is to:
                -  Create a doc via Sync Gateway
                -  Purge the doc via Sync Gateway
        3. Push a new mobile tombstone revision for the document to Sync Gateway
            - could either be with CBL, or via REST API
        4. Verify SGW does not go into infinite loop
    """

    cluster_conf = params_from_base_test_setup['cluster_config']
    cluster_topology = params_from_base_test_setup['cluster_topology']
    mode = params_from_base_test_setup['mode']
    xattrs_enabled = params_from_base_test_setup['xattrs_enabled']
    ssl_enabled = params_from_base_test_setup["ssl_enabled"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]
    sg_conf_name = 'custom_sync/grant_access_one'

    if ("sync_gateway_default_functional_tests_no_port" in sg_conf_name) and get_sg_version(cluster_conf) < "1.5.0":
        pytest.skip('couchbase/couchbases ports do not support for versions below 1.5')
    if "sync_gateway_default_functional_tests_no_port" in sg_conf_name and not ssl_enabled:
        pytest.skip('ssl disabled so cannot run without port')

    # This test should only run when using xattr meta storage
    if not xattrs_enabled:
        pytest.skip('XATTR tests require --xattrs flag')

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)
    sg_admin_url = cluster_topology['sync_gateways'][0]['admin']
    sg_url = cluster_topology['sync_gateways'][0]['public']
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None

    # bucket_name = 'data-bucket'
    # cbs_url = cluster_topology['couchbase_servers'][0]
    sg_db = 'db'
    number_docs_per_client = 1
    # number_revs_per_doc = 1

    channels = ['NASA']

    log_info('sg_conf: {}'.format(sg_conf))
    log_info('sg_admin_url: {}'.format(sg_admin_url))
    log_info('sg_url: {}'.format(sg_url))

    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_config_path=sg_conf)

    sg_client = MobileRestClient()
    mobile_user_info = UserInfo(name='mobile', password='pass', channels=channels, roles=[])
    sg_client.create_user(
        url=sg_admin_url,
        db=sg_db,
        name=mobile_user_info.name,
        password=mobile_user_info.password,
        channels=mobile_user_info.channels,
        auth=auth
    )

    mobile_auth = sg_client.create_session(
        url=sg_admin_url,
        db=sg_db,
        name=mobile_user_info.name,
        auth=auth
    )

    # Create 'number_docs_per_client' docs from Sync Gateway
    mobile_docs = document.create_docs('sg', number=number_docs_per_client, channels=mobile_user_info.channels)
    bulk_docs_resp = sg_client.add_bulk_docs(
        url=sg_url,
        db=sg_db,
        docs=mobile_docs,
        auth=mobile_auth
    )
    assert len(bulk_docs_resp) == number_docs_per_client

    sg_doc_ids = ['sg_{}'.format(i) for i in range(number_docs_per_client)]
    # sdk_doc_ids = ['sdk_{}'.format(i) for i in range(number_docs_per_client)]
    all_doc_ids = sg_doc_ids

    # Get all of the docs via Sync Gateway
    sg_docs, errors = sg_client.get_bulk_docs(url=sg_url, db=sg_db, doc_ids=all_doc_ids, auth=mobile_auth)
    assert len(sg_docs) == number_docs_per_client
    assert len(errors) == 0

    # Use Sync Gateway to delete half of the documents choosen randomly
    # deletion_count = 0
    doc_id_choice_pool = list(all_doc_ids)
    random_doc_id = random.choice(doc_id_choice_pool)

    # Get the current revision of the doc and delete it
    doc = sg_client.get_doc(url=sg_url, db=sg_db, doc_id=random_doc_id, auth=mobile_auth)
    doc_rev = doc['_rev']
    sg_client.delete_doc(url=sg_url, db=sg_db, doc_id=random_doc_id, rev=doc_rev, auth=mobile_auth)

    # Sync Gateway purge the random doc
    sg_client.purge_doc(url=sg_admin_url, db=sg_db, doc=doc, auth=auth)

    # Push a new mobile tombstone revision for the document to Sync Gateway
    # could either be with CBL, or via REST API
    with ProcessPoolExecutor() as mp:
        mp.submit(requests.delete("{}/{}/{}".format(sg_url, sg_db, random_doc_id), auth=mobile_auth, timeout=30))

    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=mobile_auth)["rows"]
    assert len(sg_docs) == 0, "sg docs are not deleted"
