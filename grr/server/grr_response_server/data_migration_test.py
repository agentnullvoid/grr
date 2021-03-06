#!/usr/bin/env python
import mock

from grr_response_core.lib import flags
from grr_response_core.lib import rdfvalue
from grr_response_core.lib.rdfvalues import client as rdf_client
from grr_response_core.lib.rdfvalues import crypto as rdf_crypto
from grr_response_server import aff4
from grr_response_server import data_migration
from grr_response_server import data_store
from grr_response_server.aff4_objects import aff4_grr
from grr_response_server.blob_stores import db_blob_store
from grr_response_server.blob_stores import memory_stream_bs
from grr_response_server.rdfvalues import objects as rdf_objects
from grr.test_lib import test_lib


class ListVfsTest(test_lib.GRRBaseTest):

  def _Touch(self, urn, content=""):
    with aff4.FACTORY.Open(
        urn, aff4_type=aff4_grr.VFSFile, mode="w", token=self.token) as fd:
      fd.Write(content)

  def testTree(self):
    client_urn = self.SetupClient(0)

    self._Touch(client_urn.Add("fs/os").Add("foo/bar/baz"), content="aaa")
    self._Touch(client_urn.Add("fs/os").Add("foo/quux/norf"), content="bbb")

    vfs = data_migration.ListVfs(client_urn)
    self.assertIn(client_urn.Add("fs/os").Add("foo"), vfs)
    self.assertIn(client_urn.Add("fs/os").Add("foo/bar"), vfs)
    self.assertIn(client_urn.Add("fs/os").Add("foo/bar/baz"), vfs)
    self.assertIn(client_urn.Add("fs/os").Add("foo/quux"), vfs)
    self.assertIn(client_urn.Add("fs/os").Add("foo/quux/norf"), vfs)

  def testVariousRoots(self):
    client_urn = self.SetupClient(0)

    self._Touch(client_urn.Add("fs/os").Add("foo"), content="foo")
    self._Touch(client_urn.Add("fs/tsk").Add("bar"), content="bar")
    self._Touch(client_urn.Add("temp").Add("foo"), content="foo")
    self._Touch(client_urn.Add("registry").Add("bar"), content="bar")

    vfs = data_migration.ListVfs(client_urn)
    self.assertIn(client_urn.Add("fs/os").Add("foo"), vfs)
    self.assertIn(client_urn.Add("fs/tsk").Add("bar"), vfs)
    self.assertIn(client_urn.Add("temp").Add("foo"), vfs)
    self.assertIn(client_urn.Add("registry").Add("bar"), vfs)


class VfsMigrationTest(test_lib.GRRBaseTest):

  def _Aff4Open(self, urn):
    return aff4.FACTORY.Open(
        urn, aff4_type=aff4_grr.VFSFile, mode="w", token=self.token)

  def testStatEntryFromSimpleFile(self):
    client_urn = self.SetupClient(0)

    with self._Aff4Open(client_urn.Add("fs/os").Add("foo")) as fd:
      stat_entry = rdf_client.StatEntry(st_mode=1337, st_size=42)
      fd.Set(fd.Schema.STAT, stat_entry)

    data_migration.MigrateClientVfs(client_urn)

    path_info = data_store.REL_DB.ReadPathInfo(
        client_id=client_urn.Basename(),
        path_type=rdf_objects.PathInfo.PathType.OS,
        components=("foo",))
    self.assertEqual(path_info.stat_entry.st_mode, 1337)
    self.assertEqual(path_info.stat_entry.st_size, 42)

  def testHashEntryFromSimpleFile(self):
    client_urn = self.SetupClient(0)

    with self._Aff4Open(client_urn.Add("fs/os").Add("foo")) as fd:
      hash_entry = rdf_crypto.Hash(md5=b"bar", sha256=b"baz")
      fd.Set(fd.Schema.HASH, hash_entry)

    data_migration.MigrateClientVfs(client_urn)

    path_info = data_store.REL_DB.ReadPathInfo(
        client_id=client_urn.Basename(),
        path_type=rdf_objects.PathInfo.PathType.OS,
        components=("foo",))
    self.assertEqual(path_info.hash_entry.md5, b"bar")
    self.assertEqual(path_info.hash_entry.sha256, b"baz")

  def testStatAndHashEntryFromSimpleFile(self):
    client_urn = self.SetupClient(0)

    with self._Aff4Open(client_urn.Add("fs/os").Add("foo")) as fd:
      stat_entry = rdf_client.StatEntry(st_mode=108)
      fd.Set(fd.Schema.STAT, stat_entry)

      hash_entry = rdf_crypto.Hash(sha256=b"quux")
      fd.Set(fd.Schema.HASH, hash_entry)

    data_migration.MigrateClientVfs(client_urn)

    path_info = data_store.REL_DB.ReadPathInfo(
        client_id=client_urn.Basename(),
        path_type=rdf_objects.PathInfo.PathType.OS,
        components=("foo",))
    self.assertEqual(path_info.stat_entry.st_mode, 108)
    self.assertEqual(path_info.hash_entry.sha256, b"quux")

  def testStatFromTree(self):
    client_urn = self.SetupClient(0)

    with self._Aff4Open(client_urn.Add("fs/os").Add("foo/bar/baz")) as fd:
      stat_entry = rdf_client.StatEntry(st_mtime=101)
      fd.Set(fd.Schema.STAT, stat_entry)

    data_migration.MigrateClientVfs(client_urn)

    path_infos = data_store.REL_DB.ReadPathInfos(
        client_id=client_urn.Basename(),
        path_type=rdf_objects.PathInfo.PathType.OS,
        components_list=[("foo",), ("foo", "bar"), ("foo", "bar", "baz")])

    self.assertEqual(path_infos[("foo",)].stat_entry.st_mtime, None)
    self.assertEqual(path_infos[("foo", "bar")].stat_entry.st_mtime, None)
    self.assertEqual(path_infos[("foo", "bar", "baz")].stat_entry.st_mtime, 101)

  def testStatHistory(self):
    datetime = rdfvalue.RDFDatetime.FromHumanReadable

    client_urn = self.SetupClient(0)
    file_urn = client_urn.Add("fs/os").Add("foo")

    with test_lib.FakeTime(datetime("2000-01-01")):
      with self._Aff4Open(file_urn) as fd:
        fd.Set(fd.Schema.STAT, rdf_client.StatEntry(st_size=10))

    with test_lib.FakeTime(datetime("2000-02-02")):
      with self._Aff4Open(file_urn) as fd:
        fd.Set(fd.Schema.STAT, rdf_client.StatEntry(st_size=20))

    with test_lib.FakeTime(datetime("2000-03-03")):
      with self._Aff4Open(file_urn) as fd:
        fd.Set(fd.Schema.STAT, rdf_client.StatEntry(st_size=30))

    data_migration.MigrateClientVfs(client_urn)

    path_info = data_store.REL_DB.ReadPathInfo(
        client_id=client_urn.Basename(),
        path_type=rdf_objects.PathInfo.PathType.OS,
        components=("foo",),
        timestamp=datetime("2000-01-10"))
    self.assertEqual(path_info.stat_entry.st_size, 10)

    path_info = data_store.REL_DB.ReadPathInfo(
        client_id=client_urn.Basename(),
        path_type=rdf_objects.PathInfo.PathType.OS,
        components=("foo",),
        timestamp=datetime("2000-02-20"))
    self.assertEqual(path_info.stat_entry.st_size, 20)

    path_info = data_store.REL_DB.ReadPathInfo(
        client_id=client_urn.Basename(),
        path_type=rdf_objects.PathInfo.PathType.OS,
        components=("foo",),
        timestamp=datetime("2000-03-30"))
    self.assertEqual(path_info.stat_entry.st_size, 30)

  def testHashHistory(self):
    datetime = rdfvalue.RDFDatetime.FromHumanReadable

    client_urn = self.SetupClient(0)
    file_urn = client_urn.Add("fs/os").Add("bar")

    with test_lib.FakeTime(datetime("2010-01-01")):
      with self._Aff4Open(file_urn) as fd:
        fd.Set(fd.Schema.HASH, rdf_crypto.Hash(md5=b"quux"))

    with test_lib.FakeTime(datetime("2020-01-01")):
      with self._Aff4Open(file_urn) as fd:
        fd.Set(fd.Schema.HASH, rdf_crypto.Hash(md5=b"norf"))

    with test_lib.FakeTime(datetime("2030-01-01")):
      with self._Aff4Open(file_urn) as fd:
        fd.Set(fd.Schema.HASH, rdf_crypto.Hash(md5=b"blargh"))

    data_migration.MigrateClientVfs(client_urn)

    path_info = data_store.REL_DB.ReadPathInfo(
        client_id=client_urn.Basename(),
        path_type=rdf_objects.PathInfo.PathType.OS,
        components=("bar",),
        timestamp=datetime("2010-12-31"))
    self.assertEqual(path_info.hash_entry.md5, b"quux")

    path_info = data_store.REL_DB.ReadPathInfo(
        client_id=client_urn.Basename(),
        path_type=rdf_objects.PathInfo.PathType.OS,
        components=("bar",),
        timestamp=datetime("2020-12-31"))
    self.assertEqual(path_info.hash_entry.md5, b"norf")

    path_info = data_store.REL_DB.ReadPathInfo(
        client_id=client_urn.Basename(),
        path_type=rdf_objects.PathInfo.PathType.OS,
        components=("bar",),
        timestamp=datetime("2030-12-31"))
    self.assertEqual(path_info.hash_entry.md5, b"blargh")


@mock.patch.object(data_migration, "_BLOB_BATCH_SIZE", 1)
class BlobStoreMigratorTest(test_lib.GRRBaseTest):

  def testBlobsAreCorrectlyMigrated(self):
    mem_bs = memory_stream_bs.MemoryStreamBlobstore()
    db_bs = db_blob_store.DbBlobstore()

    blob_contents_1 = "A" * 1024
    blob_hash_1 = mem_bs.StoreBlob(blob_contents_1)

    blob_contents_2 = "B" * 1024
    blob_hash_2 = mem_bs.StoreBlob(blob_contents_2)

    data_migration.BlobsMigrator().Execute(2)

    contents = db_bs.ReadBlob(blob_hash_1)
    self.assertEqual(contents, blob_contents_1)

    contents = db_bs.ReadBlob(blob_hash_2)
    self.assertEqual(contents, blob_contents_2)


if __name__ == "__main__":
  flags.StartMain(test_lib.main)
