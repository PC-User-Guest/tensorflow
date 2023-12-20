# Copyright 2023 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Tests for distributed save/load with the new load algorithm."""

import os
import shutil
import tempfile
import threading
import time
from typing import Callable, Optional

from absl.testing import parameterized
import numpy as np

from tensorflow.python.data.experimental.kernel_tests.service import test_base as data_service_test_base
from tensorflow.python.data.experimental.ops import data_service_ops
from tensorflow.python.data.experimental.ops import distributed_save_op
from tensorflow.python.data.kernel_tests import checkpoint_test_base
from tensorflow.python.data.kernel_tests import test_base
from tensorflow.python.data.ops import dataset_ops
from tensorflow.python.data.ops import load_op
from tensorflow.python.framework import combinations
from tensorflow.python.framework import errors
from tensorflow.python.ops import array_ops
from tensorflow.python.platform import googletest
from tensorflow.python.platform import test


class TestSnapshot:
  """Test data for snapshots."""

  def __init__(self):
    temp_dir = tempfile.mkdtemp(dir=googletest.GetTempDir())
    self.path = os.path.join(
        tempfile.mkdtemp(dir=temp_dir), "distributed_save_load_test")

  def __del__(self):
    shutil.rmtree(self.path)


class DistributedSaveLoadTest(
    data_service_test_base.TestBase, parameterized.TestCase):
  """Tests for distributed save/load with the new load algorithm."""

  @combinations.generate(
      combinations.times(
          test_base.default_test_combinations(),
          combinations.combine(
              num_workers=[1, 3],
              num_elements=[0, 10],
              num_repetitions=[1, 3],
              compression=[None, "AUTO", "GZIP"],
              max_chunk_size_bytes=[1, 16 << 10])))
  def test_save_load(
      self,
      num_workers: int,
      num_elements: int,
      num_repetitions: int,
      compression: Optional[str],
      max_chunk_size_bytes: int):
    test_snapshot = TestSnapshot()
    cluster = data_service_test_base.TestCluster(
        num_workers=num_workers,
        snapshot_max_chunk_size_bytes=max_chunk_size_bytes)
    dataset = dataset_ops.Dataset.range(num_elements)
    dataset = dataset.repeat(num_repetitions)
    self.evaluate(
        distributed_save_op.distributed_save(
            dataset, test_snapshot.path, cluster.dispatcher_address()))

    # Unlike the old load op, v2 does not need to wait for snapshot to finish.
    dataset = load_op._load_distributed_snapshot_v2(test_snapshot.path)
    self.assertDatasetProduces(
        dataset,
        list(range(num_elements)) * num_repetitions,
        assert_items_equal=True)

  @combinations.generate(
      combinations.times(
          test_base.default_test_combinations(),
          combinations.combine(num_workers=[1, 3])))
  def test_concurrent_save_load(self, num_workers: int):
    test_snapshot = TestSnapshot()
    cluster = data_service_test_base.TestCluster(num_workers=num_workers)

    def load_thread_fn():
      dataset = load_op._load_distributed_snapshot_v2(test_snapshot.path)
      self.assertDatasetProduces(
          dataset, list(range(10)), assert_items_equal=True)
    load_thread = threading.Thread(target=load_thread_fn, name="load_thread")
    load_thread.start()

    def save_thread_fn():
      time.sleep(5)
      dataset = dataset_ops.Dataset.range(10)
      self.evaluate(
          distributed_save_op.distributed_save(
              dataset, test_snapshot.path, cluster.dispatcher_address()))
    save_thread = threading.Thread(target=save_thread_fn, name="save_thread")
    save_thread.start()
    save_thread.join()
    load_thread.join()

  @combinations.generate(
      combinations.times(
          test_base.default_test_combinations(),
          combinations.combine(
              num_workers=[1, 5],
              num_elements=[10],
              num_repetitions=[10],
              max_chunk_size_bytes=[1, 16 << 10])))
  def test_deterministic_load_order(
      self,
      num_workers: int,
      num_elements: int,
      num_repetitions: int,
      max_chunk_size_bytes: int):
    """Verifies `load` produces data deterministically after `save` finishes."""
    test_snapshot = TestSnapshot()
    cluster = data_service_test_base.TestCluster(
        num_workers=num_workers,
        snapshot_max_chunk_size_bytes=max_chunk_size_bytes)
    dataset = dataset_ops.Dataset.range(num_elements).shuffle(buffer_size=10)
    self.evaluate(
        distributed_save_op.distributed_save(
            dataset, test_snapshot.path, cluster.dispatcher_address()))

    dataset = load_op._load_distributed_snapshot_v2(test_snapshot.path)
    dataset = dataset.repeat(num_repetitions)
    output = self.getDatasetOutput(dataset)
    output_per_repetition = [
        output[i : i + num_elements]
        for i in range(0, len(output), num_elements)]
    self.assertLen(output_per_repetition, num_repetitions)
    for i in range(2, num_repetitions):  # Starts from the second repetition.
      self.assertEqual(output_per_repetition[i], output_per_repetition[i - 1])

  @combinations.generate(
      combinations.times(
          test_base.default_test_combinations(),
          combinations.combine(
              num_workers=[1, 3],
              num_elements=[0, 10],
              repeated_load=[1, 5],
              sharding_policy=[
                  data_service_ops.ShardingPolicy.OFF,
                  data_service_ops.ShardingPolicy.DYNAMIC])))
  def test_distributed_load(
      self,
      num_workers: int,
      num_elements: int,
      repeated_load: int,
      sharding_policy: data_service_ops.ShardingPolicy):
    test_snapshot = TestSnapshot()
    cluster = data_service_test_base.TestCluster(num_workers=num_workers)
    dataset = dataset_ops.Dataset.range(num_elements)
    self.evaluate(
        distributed_save_op.distributed_save(
            dataset, test_snapshot.path, cluster.dispatcher_address()))

    dataset = load_op._load_distributed_snapshot_v2(test_snapshot.path)
    if repeated_load > 1:
      dataset = dataset.repeat(repeated_load)
    dataset = dataset.apply(
        data_service_ops.distribute(
            sharding_policy, cluster.dispatcher_address()))
    expected = list(range(num_elements)) * repeated_load
    if sharding_policy == data_service_ops.ShardingPolicy.OFF:
      expected *= num_workers
    self.assertDatasetProduces(dataset, expected, assert_items_equal=True)

  @combinations.generate(
      combinations.times(
          test_base.default_test_combinations(),
          combinations.combine(num_workers=[1, 3])))
  def test_save_before_sample(self, num_workers: int):
    num_elements = 10
    num_datasets = 3
    test_snapshot = TestSnapshot()
    cluster = data_service_test_base.TestCluster(num_workers=num_workers)
    datasets = [
        dataset_ops.Dataset.range(num_elements) for i in range(num_datasets)]
    for i, dataset in enumerate(datasets):
      self.evaluate(
          distributed_save_op.distributed_save(
              dataset,
              os.path.join(test_snapshot.path, f"dataset_{i}"),
              cluster.dispatcher_address()))

    loaded_datasets = []
    for i in range(len(datasets)):
      loaded_datasets.append(
          load_op._load_distributed_snapshot_v2(
              os.path.join(test_snapshot.path, f"dataset_{i}")))
    dataset = dataset_ops.Dataset.sample_from_datasets(
        loaded_datasets,
        weights=[1.0] * num_datasets,
        stop_on_empty_dataset=False)
    self.assertDatasetProduces(
        dataset,
        list(range(num_elements)) * num_datasets,
        assert_items_equal=True)

  @combinations.generate(
      combinations.times(
          test_base.default_test_combinations(),
          combinations.combine(num_workers=[1, 3], num_repetitions=[1, 3])))
  def test_save_after_sample(self, num_workers: int, num_repetitions: int):
    num_elements = 10
    num_datasets = 3
    test_snapshot = TestSnapshot()
    cluster = data_service_test_base.TestCluster(num_workers=num_workers)
    datasets = [
        dataset_ops.Dataset.range(num_elements) for i in range(num_datasets)]
    if num_repetitions > 1:
      datasets = [dataset.repeat(num_repetitions) for dataset in datasets]
    dataset = dataset_ops.Dataset.sample_from_datasets(
        datasets, weights=[1.0] * num_datasets, stop_on_empty_dataset=False)
    self.evaluate(
        distributed_save_op.distributed_save(
            dataset, test_snapshot.path, cluster.dispatcher_address()))

    dataset = load_op._load_distributed_snapshot_v2(test_snapshot.path)
    self.assertDatasetProduces(
        dataset,
        list(range(num_elements)) * num_datasets * num_repetitions,
        assert_items_equal=True)

  @combinations.generate(
      combinations.times(
          test_base.default_test_combinations(),
          combinations.combine(num_workers=[1, 3])))
  def test_enumerate(self, num_workers: int):
    test_snapshot = TestSnapshot()
    cluster = data_service_test_base.TestCluster(num_workers)
    dataset = dataset_ops.Dataset.from_tensor_slices(["a", "b", "c"])
    dataset = dataset.repeat(3)
    dataset = dataset.enumerate()
    self.evaluate(
        distributed_save_op.distributed_save(
            dataset, test_snapshot.path, cluster.dispatcher_address()))

    dataset = load_op._load_distributed_snapshot_v2(test_snapshot.path)
    indexes, elements = map(list, zip(*self.getDatasetOutput(dataset)))
    if num_workers == 1:
      self.assertCountEqual(indexes, list(range(9)))
    self.assertCountEqual(elements, [b"a", b"b", b"c"] * 3)

  @combinations.generate(test_base.default_test_combinations())
  def test_worker_failure(self):
    test_snapshot = TestSnapshot()
    cluster = data_service_test_base.TestCluster(num_workers=1)
    components = np.array([1.0, 2.0, 3.0, np.nan, 5.0]).astype(np.float32)
    dataset = dataset_ops.Dataset.from_tensor_slices(components)
    dataset = dataset.map(lambda x: array_ops.check_numerics(x, "message"))
    self.evaluate(
        distributed_save_op.distributed_save(
            dataset, test_snapshot.path, cluster.dispatcher_address()))

    with self.assertRaises(errors.InvalidArgumentError):
      dataset = load_op._load_distributed_snapshot_v2(test_snapshot.path)
      self.getDatasetOutput(dataset)

  @combinations.generate(
      combinations.times(
          test_base.default_test_combinations(),
          combinations.combine(num_repetitions=[None, 0, 1, 3])))
  def test_snapshot_chunks_cardinality(self, num_repetitions: int):
    test_snapshot = TestSnapshot()
    cluster = data_service_test_base.TestCluster(num_workers=1)
    dataset = dataset_ops.Dataset.range(10)
    self.evaluate(
        distributed_save_op.distributed_save(
            dataset, test_snapshot.path, cluster.dispatcher_address()))

    dataset = load_op._ListSnapshotChunksDataset(test_snapshot.path)
    if num_repetitions != 1:
      dataset = dataset.repeat(num_repetitions)

    while self.evaluate(dataset.cardinality()) == dataset_ops.UNKNOWN:
      time.sleep(.1)
    expected = (
        dataset_ops.INFINITE if num_repetitions is None else num_repetitions)
    self.assertEqual(self.evaluate(dataset.cardinality()), expected)


class SaveLoadCheckpointTest(
    data_service_test_base.TestBase,
    checkpoint_test_base.CheckpointTestBase,
    parameterized.TestCase):

  @combinations.generate(
      combinations.times(
          test_base.default_test_combinations(),
          checkpoint_test_base.default_test_combinations()))
  def test_save_load_checkpoint(self, verify_fn: Callable[..., None]):
    test_snapshot = TestSnapshot()
    cluster = data_service_test_base.TestCluster(num_workers=1)
    dataset = dataset_ops.Dataset.range(10)
    self.evaluate(
        distributed_save_op.distributed_save(
            dataset, test_snapshot.path, cluster.dispatcher_address()))

    def _build_ds() -> dataset_ops.Dataset:
      return load_op._load_distributed_snapshot_v2(test_snapshot.path)

    verify_fn(self, _build_ds, num_outputs=10)


if __name__ == "__main__":
  test.main()
