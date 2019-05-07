# Copyright 2017 Google Inc. All Rights Reserved.
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
"""Logic associated with schema inference and propagation.

This module contains functionality to set the schema assciated with a Tensor,
and to infer the schema for a tensor, including any information that has been
set.  This module will also contain any schema propagation logic, i.e. deducing
the schema of a tensor from its parents in the graph.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections

# GOOGLE-INITIALIZATION

import six
import tensorflow as tf
from tensorflow_transform.tf_metadata import dataset_schema
from tensorflow_transform.tf_metadata import schema_utils

from google.protobuf import any_pb2

from tensorflow_metadata.proto.v0 import schema_pb2

ANNOTATION_PREFIX_URL = 'type.googleapis.com'


def _feature_spec_from_batched_tensors(tensors):
  """Infer `Schema` proto from a dict of tensors.

  Args:
    tensors: A dict whose keys are strings and values are `Tensor` or
      `SparseTensor`s.

  Returns:
    A `Schema` proto inferred from the types and shapes of the tensors.

  Raises:
    ValueError: If the feature spec cannot be inferred.
    TypeError: If any of the values of `tensors` are not a `Tensor` or
        `SparseTensor`.
  """
  result = {}
  for name, tensor in six.iteritems(tensors):
    tensor = tensors[name]
    shape = tensor.get_shape()
    if tensor.dtype not in (tf.string, tf.int64, tf.float32):
      raise ValueError('{} had invalid dtype {} for feature spec'.format(
          tensor, tensor.dtype))
    if isinstance(tensor, tf.SparseTensor):
      if shape.ndims != 2:
        raise ValueError(
            '{} had invalid shape {} for VarLenFeature: must have rank '
            '2'.format(tensor, shape))
      result[name] = tf.io.VarLenFeature(tensor.dtype)
    elif isinstance(tensor, tf.Tensor):
      if shape.ndims in [None, 0]:
        raise ValueError(
            '{} had invalid shape {} for FixedLenFeature: must have rank '
            'at least 1'.format(tensor, shape))
      if any(dim is None for dim in shape.as_list()[1:]):
        raise ValueError(
            '{} had invalid shape {} for FixedLenFeature: apart from the batch '
            'dimension, all dimensions must have known size'.format(
                tensor, shape))
      result[name] = tf.io.FixedLenFeature(shape.as_list()[1:], tensor.dtype)
    else:
      raise TypeError(
          'Expected a Tensor or SparseTensor, got {} of type {}'.format(
              tensor, type(tensor)))

  return result


def infer_feature_schema(features, graph, session=None):
  """Given a dict of tensors, creates a `Schema`.

  Infers a schema, in the format of a tf.Transform `Schema`, for the given
  dictionary of tensors.

  If there is an override specified, we override the inferred schema for the
  given feature's tensor.  An override has the meaning that we should set
  is_categorical=True.  If session is not provided then we just set
  is_categorical=True, and if the session is provided then was also compute
  values of the tensors representing the min and max values and set them in the
  schema.

  If annotations have been specified, they are added to the output schema.

  Args:
    features: A dict mapping column names to `Tensor` or `SparseTensor`s. The
      `Tensor` or `SparseTensor`s should have a 0'th dimension which is
      interpreted as the batch dimension.
    graph: A `tf.Graph` used to determine schema overrides.
    session: (optional) A `tf.Session` used to compute schema overrides.  If
      None, schema overrides will not be computed.

  Returns:
    A `Schema` object.
  """
  tensor_ranges = _get_tensor_schema_overrides(graph)
  if session is None:
    tensor_ranges = {tensor: (None, None) for tensor in tensor_ranges.keys()}
    tensor_annotations = {}
    global_annotations = []
  else:
    tensor_ranges = session.run(tensor_ranges)
    tensor_annotations, global_annotations = _get_schema_annotations(
        graph, session)

  domains = {}
  feature_annotations = {}
  for name, tensor in six.iteritems(features):
    values = tensor.values if isinstance(tensor, tf.SparseTensor) else tensor
    if values in tensor_ranges:
      assert values.dtype == tf.int64
      min_value, max_value = tensor_ranges[values]
      domains[name] = schema_pb2.IntDomain(
          min=min_value, max=max_value, is_categorical=True)
    annotation = tensor_annotations.get(values)
    if annotation is not None:
      feature_annotations[name] = annotation
  feature_spec = _feature_spec_from_batched_tensors(features)

  schema_proto = schema_utils.schema_from_feature_spec(feature_spec, domains,
                                                       feature_annotations,
                                                       global_annotations)
  return dataset_schema.Schema(schema_proto)


# Names of collections, which should all be the same length and contain tensors.
# Each tensor in the first collection should have its min/max described by the
# tensors in the other two collections.
_TF_METADATA_TENSOR_COLLECTION = 'tft_schema_override_tensor'
_TF_METADATA_TENSOR_MIN_COLLECTION = 'tft_schema_override_min'
_TF_METADATA_TENSOR_MAX_COLLECTION = 'tft_schema_override_max'
# Collections for adding to annotation.extra_metadata on the schema. Each
# tensor in the first collection should have a proto type and proto message in
# the other two collections
_TF_METADATA_EXTRA_ANNOTATION = 'tft_schema_override_annotation_tensor'
_TF_METADATA_EXTRA_ANNOTATION_TYPE_URL = 'tft_schema_override_annotation_type'
_TF_METADATA_EXTRA_ANNOTATION_PROTO = 'tft_schema_override_annotation_proto'
# Used to indicate that an annotation should be applied at the schema level.
_TF_METADATA_EXTRA_ANNOTATION_GLOBAL = 'tft_schema_override_global_sentinel'


def set_tensor_schema_override(tensor, min_value, max_value):
  """Override parts of the schema of a `Tensor`.

  Args:
    tensor: The `Tensor` whose range is being set.  Must have dtype int64.
    min_value: A `Tensor` representing the min value of `tensor`.
    max_value: A `Tensor` representing the max value of `tensor`.

  Raises:
    ValueError: If any arguments are invalid.
  """
  if not isinstance(tensor, tf.Tensor):
    raise ValueError('tensor {} was not a Tensor'.format(tensor))
  if tensor.dtype != tf.int64:
    raise ValueError(
        'Range can only be set for feature of type tf.int64, got {}'.format(
            tensor.dtype))
  if not isinstance(min_value, tf.Tensor):
    raise ValueError('min_value {} was not a Tensor'.format(min_value))
  if not isinstance(max_value, tf.Tensor):
    raise ValueError('max_value {} was not a Tensor'.format(max_value))
  tf.compat.v1.add_to_collection(_TF_METADATA_TENSOR_COLLECTION, tensor)
  tf.compat.v1.add_to_collection(_TF_METADATA_TENSOR_MIN_COLLECTION, min_value)
  tf.compat.v1.add_to_collection(_TF_METADATA_TENSOR_MAX_COLLECTION, max_value)


def _get_tensor_schema_overrides(graph):
  """Lookup overrides for `Tensor`s  or `SparseTensor`s."""
  tensors = graph.get_collection(_TF_METADATA_TENSOR_COLLECTION)
  min_values = graph.get_collection(_TF_METADATA_TENSOR_MIN_COLLECTION)
  max_values = graph.get_collection(_TF_METADATA_TENSOR_MAX_COLLECTION)
  assert len(tensors) == len(min_values), '{} != {}'.format(tensors, min_values)
  assert len(tensors) == len(max_values), '{} != {}'.format(tensors, max_values)
  return dict(zip(tensors, zip(min_values, max_values)))


def annotate(type_url, proto_message, tensor=None):
  """Adds a deferred annotation to the schema.

  Experimental: This API is subject to change.

  This function allows analyzers or end users to annotate the post-transform
  schema with additional information based on analyzer output. These annotations
  are stored in the annotation.extra_metadata field of the tf.metadata schema:
  https://github.com/tensorflow/metadata/blob/master/tensorflow_metadata/proto/v0/schema.proto#L193

  Args:
    type_url: Uniquely identifies the type of the serialized proto message. See
      https://github.com/protocolbuffers/protobuf/blob/master/src/google/protobuf/any.proto#L151
    proto_message: The proto value to add to the schema. Can be either a real
      serialized proto value or a deferred tensor containing the serialized
      proto to write. If the proto value is empty, this will be a no-op.
    tensor: (optional) If provided, the annotation will be written to the
      Feature proto that is created for this tensor in the schema. If None,
      the annotation is assumed to be global. Note: if the tensor is not present
        in the output signature of `preprocessing_fn`, this will be a no-op.
  """
  if tensor is None:
    tensor = tf.constant('unused', name=_TF_METADATA_EXTRA_ANNOTATION_GLOBAL)
  # Note: The tensors, types, and messages are stored in separate collections
  # because SavedModel only supports primitive types in collections.
  tf.compat.v1.add_to_collection(_TF_METADATA_EXTRA_ANNOTATION, tensor)
  tf.compat.v1.add_to_collection(_TF_METADATA_EXTRA_ANNOTATION_TYPE_URL,
                                 type_url)
  tf.compat.v1.add_to_collection(_TF_METADATA_EXTRA_ANNOTATION_PROTO,
                                 proto_message)


def _get_schema_annotations(graph, session):
  """Fetch extra_metadata annotations to be applied to the schema.

  Extracts any deferred annotations that have been added to the graph and
  evaluates them to obtain any_pb2.Any proto messages.

  Args:
    graph: A `tf.Graph` used to determine schema overrides.
    session: (optional) A `tf.Session` used to compute schema annotations.  If
      None, schema annotations will not be computed.

  Returns:
    tensor_annotations: dictionary from tensor to list of any_pb2.Any protos to
      be added as an annotation for that tensor's feature in the schema.
    global_annotations: list of any_pb2.Any protos to be added at the global
      schema level.
  """
  tensor_annotations = collections.defaultdict(list)
  global_annotations = []

  for (tensor, type_url, proto_value) in zip(
      graph.get_collection(_TF_METADATA_EXTRA_ANNOTATION),
      graph.get_collection(_TF_METADATA_EXTRA_ANNOTATION_TYPE_URL),
      graph.get_collection(_TF_METADATA_EXTRA_ANNOTATION_PROTO)):
    if isinstance(proto_value, tf.Tensor):
      proto_value = session.run(proto_value)
    if not proto_value:
      continue
    annotation = any_pb2.Any(type_url=type_url, value=proto_value)
    # Entries meant for the global schema annotation will have names like
    # tft_schema_override_global_sentinel:0 or
    # transform/tft_schema_override_global_sentinel_1:0
    tensor_name = tensor.name.split('/')[-1]
    if tensor_name.startswith(_TF_METADATA_EXTRA_ANNOTATION_GLOBAL):
      global_annotations.append(annotation)
    else:
      tensor_annotations[tensor].append(annotation)
  return tensor_annotations, global_annotations
