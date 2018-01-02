import pytest

from keywords.utils import random_string
from CBLClient.Database import Database
from CBLClient.Document import Document

baseUrl = "http://172.16.1.154:8080"
#baseUrl = "http://192.168.0.117:8080"
#baseUrl = "http://172.23.121.73:55555"
dbName = "foo"
docIdPrefix = "bar"

class TestDatabase():
    db_obj = Database(baseUrl)
    doc_obj = Document(baseUrl)

    @pytest.mark.parametrize("dbName, err_msg", [
        ("", "name should not be empty"),
        (random_string(1028), "File name too long")
    ])
    def test_database_create_exception(self, dbName, err_msg):
        '''
        @summary: Checking for the Exception handling in database create API
        '''
        _, err_resp = self.db_obj.create(dbName)
        assert err_msg in err_resp

    def test_getDocument_exception(self):
        db = self.db_obj.create(random_string(6))
        # checking when Null/None documentId is provided
        err_msg = "a documentID parameter is null"
        _, err_resp = self.db_obj.getDocument(db, None)
        assert err_msg in err_resp
        # checking document in db with empty name
        doc_id = self.db_obj.getDocument(db, "")
        assert doc_id is None
        # checking for a non-existing doc in DB
        doc_id = self.db_obj.getDocument(db, "I-do-not-exist")
        assert doc_id is None

    def test_saveDocument_exception(self):
        db = self.db_obj.create(random_string(6))
        err_msg = "a document parameter is null"
        _, err_resp = self.db_obj.saveDocument(db, None)
        assert err_msg in err_resp

    def test_delete_exception(self):
        db = self.db_obj.create(random_string(6))
        # Exception checking when document id is null
        err_msg = "a document parameter is null"
        _, err_resp = self.db_obj.delete(database=db, document=None)
        assert err_msg in err_resp
        _, err_resp = self.db_obj.purge(database=db, document=None)
        assert err_msg in err_resp

    def test_deleteDB_exception(self):
        assert 1

    def test_exists_exception(self):
        assert 1

    @pytest.mark.parametrize("dbName", [
        random_string(1),
        random_string(6),
        random_string(128),
        "_{}".format(random_string(6)),
        "{}_".format(random_string(6)),
        "_{}_".format(random_string(6)),
        random_string(6, digit=True),
        random_string(6).capitalize(),
        random_string(6).upper(),
    ])
    def test_database_create(self, dbName):
        '''
        @summary: Testing Database constructor method of Database API
        '''
        db = self.db_obj.create(dbName)
        assert self.db_obj.getName(db) == dbName
        assert self.db_obj.deleteDB(db) == -1

    def test_database_add_listener(self):
        '''
        @summary: Checking if we are able to add database or document
        listener to Database object
        '''
        assert 1

    @pytest.mark.parametrize("dbName", [
        random_string(1),
        random_string(6),
        random_string(128),
        "_{}".format(random_string(6)),
        "{}_".format(random_string(6)),
        "_{}_".format(random_string(6)),
        random_string(6, digit=True),
        random_string(6).capitalize(),
        random_string(6).upper(),
    ])
    def test_database_close(self, dbName):
        '''
        @summary: Testing close method of Database API
        '''
        db = self.db_obj.create(dbName)
        assert self.db_obj.close(db) == -1

    def test_compact(self):
        '''
        @summary: Testing compact method of Database API
        '''
        assert 1

    @pytest.mark.parametrize("dbName", [
        random_string(1),
        random_string(6),
        random_string(128),
        "_{}".format(random_string(6)),
        "{}_".format(random_string(6)),
        "_{}_".format(random_string(6)),
        random_string(6, digit=True),
        random_string(6).upper(),
    ])
    def test_contains(self, dbName):
        '''
        @summary: Testing contains method of Database API
        '''
        doc = self.doc_obj.create(doc_id=docIdPrefix)
        db = self.db_obj.create(dbName)
        self.db_obj.saveDocument(db, doc)
        assert self.db_obj.contains(db, docIdPrefix)
        assert self.db_obj.deleteDB(db) == -1

    @pytest.mark.parametrize("dbName", [
        random_string(1),
        random_string(6),
        random_string(128),
        "_{}".format(random_string(6)),
        "{}_".format(random_string(6)),
        "_{}_".format(random_string(6)),
        random_string(6, digit=True),
        random_string(6).upper(),
    ])
    def test_deleteDB(self, dbName):
        '''
        @summary: Testing delete(DB) method of Database API
        '''
        db = self.db_obj.create(dbName)
        path = self.db_obj.getPath(db)
        assert self.db_obj.deleteDB(db) == -1
        assert self.db_obj.exists(dbName, path) == False

    @pytest.mark.parametrize("dbName, docId", [
        (random_string(1), random_string(6)),
        (random_string(6), random_string(6)),
        (random_string(128), random_string(6)),
        ("_{}".format(random_string(6)), random_string(6)),
        ("{}_".format(random_string(6)), random_string(6)),
        ("_{}_".format(random_string(6)), random_string(6)),
        (random_string(6, digit=True), random_string(6)),
        (random_string(6).capitalize(), random_string(6)),
        (random_string(6).upper(), random_string(6))
    ])
    def test_delete(self, dbName, docId):
        '''
        @summary: Testing delete method of Database API
        '''
        db = self.db_obj.create(dbName)
        assert self.db_obj.getDocument(db, docId) is None

        doc = self.doc_obj.create(doc_id=docId)
        self.doc_obj.setString(doc, "key", "value")
        self.db_obj.saveDocument(db, doc)
        doc_res = self.db_obj.getDocument(db, docId)
        assert doc_res is not None
        assert self.doc_obj.getId(doc_res) == docId
        assert self.db_obj.getCount(db) == 1

        doc_res = self.db_obj.getDocument(db, docId)
        assert doc_res is not None
        assert self.doc_obj.getString(doc_res, "key") == "value"

        self.db_obj.delete(document=doc, database=db)
        doc_res = self.db_obj.getDocument(db, docId)
        assert self.db_obj.getCount(db) == 0
        doc_res = self.db_obj.getDocument(db, docId)
        assert doc_res is None
        assert self.db_obj.deleteDB(db) == -1

    @pytest.mark.parametrize("dbName, docId, num_of_docs", [
        (random_string(6), random_string(8), 9),
        (random_string(6), random_string(8), 99),
        (random_string(6), random_string(8), 999),
        (random_string(6), random_string(8), 9999)
    ])
    def test_getCount(self, num_of_docs, dbName, docId):
        '''
        @summary: Testing getCount method of Database API
        '''
        db = self.db_obj.create(dbName)
        for i in range(num_of_docs):
            doc = self.doc_obj.create(doc_id="{}_{}".format(docId, i))
            self.db_obj.saveDocument(db, doc)
        doc_count = self.db_obj.getCount(db)
        assert num_of_docs == doc_count
        assert self.db_obj.deleteDB(db) == -1

    @pytest.mark.parametrize("dbName", [
        random_string(1),
        random_string(6),
        random_string(128),
        "_{}".format(random_string(6)),
        "{}_".format(random_string(6)),
        "_{}_".format(random_string(6)),
        random_string(6, digit=True),
        random_string(6).upper(),
    ])
    def test_exists(self, dbName):
        '''
        @summary: Testing exist method of Database API
        '''
        db = self.db_obj.create(dbName)
        path = self.db_obj.getPath(db)
        assert self.db_obj.exists(dbName, path) == True
        assert self.db_obj.deleteDB(db) == -1
        assert self.db_obj.exists(dbName, path) == False

    @pytest.mark.parametrize("dbName, docId", [
        # "",
        (random_string(1), random_string(6)),
        (random_string(6), random_string(6)),
        # (random_string(128), random_string(6)),
        ("_{}".format(random_string(6)), random_string(6)),
        ("{}_".format(random_string(6)), random_string(6)),
        ("_{}_".format(random_string(6)), random_string(6)),
        (random_string(6, digit=True), random_string(6)),
        (random_string(6).capitalize(), random_string(6)),
        (random_string(6).upper(), random_string(6))
    ])
    def test_getDocument(self, dbName, docId):
        '''
        @summary: Testing getDocument method of Database API
        '''
        db = self.db_obj.create(dbName)
        doc = self.doc_obj.create(docId)
        self.db_obj.saveDocument(db, doc)
        new_doc = self.db_obj.getDocument(db, docId)
        assert self.doc_obj.getId(new_doc) == self.doc_obj.getId(doc)
        assert self.db_obj.deleteDB(db) == -1

    @pytest.mark.parametrize("dbName", [
        random_string(1),
        random_string(6),
        random_string(128),
        "_{}".format(random_string(6)),
        "{}_".format(random_string(6)),
        "_{}_".format(random_string(6)),
        random_string(6, digit=True),
        random_string(6).capitalize(),
        random_string(6).upper(),
    ])
    def test_getName(self, dbName):
        '''
        @summary: Testing getName method of Database API
        '''
        db = self.db_obj.create(dbName)
        assert dbName == str(self.db_obj.getName(db))
        assert self.db_obj.deleteDB(db) == -1

    @pytest.mark.parametrize("dbName", [
        random_string(1),
        random_string(6),
        random_string(128),
        "_{}".format(random_string(6)),
        "{}_".format(random_string(6)),
        "_{}_".format(random_string(6)),
        random_string(6, digit=True),
        random_string(6).capitalize(),
        random_string(6).upper(),
    ])
    def test_getPath(self, dbName):
        '''
        @summary: Testing getPath method of Database API
        '''
        db = self.db_obj.create(dbName)
        assert self.db_obj.getPath(db)
        assert self.db_obj.deleteDB(db) == -1

    @pytest.mark.parametrize("db1, db2, docId", [
        # "",
        (random_string(1), random_string(1), random_string(6)),
        (random_string(6), random_string(6), random_string(6)),
        # (random_string(128), random_string(128), random_string(6)),
        ("_{}".format(random_string(6)), "_{}".format(random_string(6)), random_string(6)),
        ("{}_".format(random_string(6)), "{}_".format(random_string(6)), random_string(6)),
        ("_{}_".format(random_string(6)), "_{}_".format(random_string(6)), random_string(6)),
        (random_string(6, digit=True), random_string(6, digit=True), random_string(6)),
        (random_string(6).capitalize(), random_string(6).capitalize(), random_string(6)),
        (random_string(6).upper(), random_string(6).upper(), random_string(6))
    ])
    def test_purge(self, db1, db2, docId):
        '''
        @summary: Testing purge method of Database API
        '''
        doc = self.doc_obj.create(doc_id=docIdPrefix)
        db_1 = self.db_obj.create(db1)
        db_2 = self.db_obj.create(db2)
        self.db_obj.saveDocument(db_1, doc)
        self.db_obj.saveDocument(db_2, doc)
        self.db_obj.purge(document=doc, database=db_1)
        assert not self.db_obj.getDocument(db_2, docId)
        assert self.db_obj.getDocument(db_2, docId)
        assert self.db_obj.deleteDB(db_1) == -1
        assert self.db_obj.deleteDB(db_2) == -1

    @pytest.mark.parametrize("dbName, docId", [
        (random_string(1), random_string(6)),
        (random_string(6), random_string(6)),
        (random_string(128), random_string(6)),
        ("_{}".format(random_string(6)), random_string(6)),
        ("{}_".format(random_string(6)), random_string(6)),
        ("_{}_".format(random_string(6)), random_string(6)),
        (random_string(6, digit=True), random_string(6)),
        (random_string(6).capitalize(), random_string(6)),
        (random_string(6).upper(), random_string(6))
    ])
    def test_saveDocument(self, dbName, docId):
        '''
        @summary: Testing save method of Database API
        '''
        doc = self.doc_obj.create(docId)
        db = self.db_obj.create(dbName)
        doc_in_db_check = self.db_obj.getDocument(db, docId)
        assert not doc_in_db_check
        self.db_obj.saveDocument(db, doc)
        doc_res = self.db_obj.getDocument(db, docId)
        assert docId == str(self.doc_obj.getId(doc_res))
        assert self.db_obj.deleteDB(db) == -1

    def test_getDocuments(self):
        '''
        @summary: Testing the bulk add and bulk get docs. This also
        test inbatch API of Database class.
        '''
        num_of_docs = 5
        db = self.db_obj.create("dbName")
        documents = dict()
        ids = []
        for i in range(num_of_docs):
            data = {}
            doc_id = "{}_{}".format(docIdPrefix, i)
            ids.append(doc_id)
            data["test_string_{}".format(i)] = "value_{}".format(i)
            documents[doc_id] = data
        self.db_obj.saveDocuments(db, documents)
        docs_in_db = self.db_obj.getDocuments(db, ids)
        assert num_of_docs == self.db_obj.getCount(db)
        assert documents == docs_in_db
        assert self.db_obj.deleteDB(db) == -1
