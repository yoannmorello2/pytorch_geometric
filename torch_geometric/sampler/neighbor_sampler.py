from typing import Any, Dict, Optional, Tuple, Union

import torch
from torch import Tensor

from torch_geometric.data import Data, HeteroData, remote_backend_utils
from torch_geometric.data.feature_store import FeatureStore
from torch_geometric.data.graph_store import GraphStore
from torch_geometric.sampler.base import (
    BaseSampler,
    EdgeSamplerInput,
    NodeSamplerInput,
    HeteroSamplerOutput,
    SamplerOutput,
)
from torch_geometric.sampler.utils import (
    add_negative_samples,
    set_node_time_dict,
    to_csc,
    to_hetero_csc,
)
from torch_geometric.typing import NumNeighbors


class NeighborSampler(BaseSampler):
    r"""An implementation of an in-memory neighbor sampler."""
    def __init__(
        self,
        data: Union[Data, HeteroData, Tuple[FeatureStore, GraphStore]],
        num_neighbors: NumNeighbors,
        replace: bool = False,
        directed: bool = True,
        input_type: Optional[Any] = None,
        time_attr: Optional[str] = None,
        is_sorted: bool = False,
        share_memory: bool = False,
    ):
        self.data_cls = data.__class__ if isinstance(
            data, (Data, HeteroData)) else 'custom'
        self.num_neighbors = num_neighbors
        self.replace = replace
        self.directed = directed
        self.node_time = None
        self.input_type = input_type

        # TODO if self.edge_time is not None and
        # `src` or `dst` nodes don't have time attribute
        # i.e node_time_dict[input_type[0/-1]] doesn't exist
        # set it to largest representable torch.long.

        # Set the number of source and destination nodes if we can, otherwise
        # ignore:
        self.num_src_nodes, self.num_dst_nodes = None, None
        if self.data_cls != 'custom' and issubclass(self.data_cls, Data):
            self.num_src_nodes = self.num_dst_nodes = data.num_nodes
        elif isinstance(self.input_type, tuple):
            if self.data_cls == 'custom':
                out = remote_backend_utils.size(*data, self.input_type)
                self.num_src_nodes, self.num_dst_nodes = out
            else:  # issubclass(self.data_cls, HeteroData):
                self.num_src_nodes = data[self.input_type[0]].num_nodes
                self.num_dst_nodes = data[self.input_type[-1]].num_nodes

        # TODO Unify the following conditionals behind the `FeatureStore`
        # and `GraphStore` API:

        # If we are working with a `Data` object, convert the edge_index to
        # CSC and store it:
        if isinstance(data, Data):
            if time_attr is not None:
                # TODO `time_attr` support for homogeneous graphs
                raise ValueError(
                    f"'time_attr' attribute not yet supported for "
                    f"'{data.__class__.__name__}' object")

            # Convert the graph data into a suitable format for sampling.
            out = to_csc(data, device='cpu', share_memory=share_memory,
                         is_sorted=is_sorted)
            self.colptr, self.row, self.perm = out
            assert isinstance(num_neighbors, (list, tuple))

        # If we are working with a `HeteroData` object, convert each edge
        # type's edge_index to CSC and store it:
        elif isinstance(data, HeteroData):
            if time_attr is not None:
                self.node_time_dict = data.collect(time_attr)
            else:
                self.node_time_dict = None

            self.node_types, self.edge_types = data.metadata()
            self._set_num_neighbors_and_num_hops(num_neighbors)

            assert input_type is not None
            self.input_type = input_type

            # Obtain CSC representations for in-memory sampling:
            out = to_hetero_csc(data, device='cpu', share_memory=share_memory,
                                is_sorted=is_sorted)
            colptr_dict, row_dict, perm_dict = out

            # Conversions to/from C++ string type:
            # Since C++ cannot take dictionaries with tuples as key as input,
            # edge type triplets need to be converted into single strings. This
            # is done by maintaining the following mappings:
            self.to_rel_type = {key: '__'.join(key) for key in self.edge_types}
            self.to_edge_type = {
                '__'.join(key): key
                for key in self.edge_types
            }

            # TODO(manan): drop remapping keys in perm_dict, so we can remove
            # this logic from NeighborLoader as well.
            self.row_dict = remap_keys(row_dict, self.to_rel_type)
            self.colptr_dict = remap_keys(colptr_dict, self.to_rel_type)
            self.perm_dict = remap_keys(perm_dict, self.to_rel_type)
            self.num_neighbors = remap_keys(self.num_neighbors,
                                            self.to_rel_type)

        # If we are working with a `Tuple[FeatureStore, GraphStore]` object,
        # obtain edges from GraphStore and convert them to CSC if necessary,
        # storing the resulting representations:
        elif isinstance(data, tuple):
            # TODO support `FeatureStore` with no edge types (e.g. `Data`)
            feature_store, graph_store = data

            # TODO support `collect` on `FeatureStore`:
            self.node_time_dict = None
            if time_attr is not None:
                # We need to obtain all features with 'attr_name=time_attr'
                # from the feature store and store them in node_time_dict. To
                # do so, we make an explicit feature store GET call here with
                # the relevant 'TensorAttr's
                time_attrs = [
                    attr for attr in feature_store.get_all_tensor_attrs()
                    if attr.attr_name == time_attr
                ]
                for attr in time_attrs:
                    attr.index = None
                time_tensors = feature_store.multi_get_tensor(time_attrs)
                self.node_time_dict = {
                    time_attr.group_name: time_tensor
                    for time_attr, time_tensor in zip(time_attrs, time_tensors)
                }

            # Obtain all node and edge metadata:
            node_attrs = feature_store.get_all_tensor_attrs()
            edge_attrs = graph_store.get_all_edge_attrs()

            self.node_types = list(
                set(node_attr.group_name for node_attr in node_attrs))
            self.edge_types = list(
                set(edge_attr.edge_type for edge_attr in edge_attrs))

            self._set_num_neighbors_and_num_hops(num_neighbors)

            assert input_type is not None
            self.input_type = input_type

            # Obtain CSC representations for in-memory sampling:
            row_dict, colptr_dict, perm_dict = graph_store.csc()

            self.to_rel_type = {key: '__'.join(key) for key in self.edge_types}
            self.to_edge_type = {
                '__'.join(key): key
                for key in self.edge_types
            }
            self.row_dict = remap_keys(row_dict, self.to_rel_type)
            self.colptr_dict = remap_keys(colptr_dict, self.to_rel_type)
            self.perm_dict = remap_keys(perm_dict, self.to_rel_type)
            self.num_neighbors = remap_keys(self.num_neighbors,
                                            self.to_rel_type)
        else:
            raise TypeError(
                f'{self.__class__.__name__} found invalid type: {type(data)}')

    def _set_num_neighbors_and_num_hops(self, num_neighbors):
        if isinstance(num_neighbors, (list, tuple)):
            self.num_neighbors = {
                key: num_neighbors
                for key in self.edge_types
            }
        assert isinstance(self.num_neighbors, dict)

        # Add at least one element to the list to ensure `max` is well-defined
        self.num_hops = max([0] +
                            [len(v) for v in self.num_neighbors.values()])

    def _sparse_neighbor_sample(self, index: Tensor):
        fn = torch.ops.torch_sparse.neighbor_sample
        node, row, col, edge = fn(
            self.colptr,
            self.row,
            index,
            self.num_neighbors,
            self.replace,
            self.directed,
        )
        return node, row, col, edge

    def _hetero_sparse_neighbor_sample(
        self,
        index_dict: Dict[str, Tensor],
        **kwargs,
    ):
        if self.node_time_dict is None:
            fn = torch.ops.torch_sparse.hetero_neighbor_sample
            node_dict, row_dict, col_dict, edge_dict = fn(
                self.node_types,
                self.edge_types,
                self.colptr_dict,
                self.row_dict,
                index_dict,
                self.num_neighbors,
                self.num_hops,
                self.replace,
                self.directed,
            )
        else:
            try:
                fn = torch.ops.torch_sparse.hetero_temporal_neighbor_sample
            except RuntimeError as e:
                raise RuntimeError(
                    "The 'torch_sparse' operator "
                    "'hetero_temporal_neighbor_sample' was not "
                    "found. Please upgrade your 'torch_sparse' installation "
                    "to 0.6.15 or greater to use this feature.") from e

            node_dict, row_dict, col_dict, edge_dict = fn(
                self.node_types,
                self.edge_types,
                self.colptr_dict,
                self.row_dict,
                index_dict,
                self.num_neighbors,
                kwargs.get('node_time_dict', self.node_time_dict),
                self.num_hops,
                self.replace,
                self.directed,
            )
        return node_dict, row_dict, col_dict, edge_dict

    # Node-based sampling #####################################################

    def sample_from_nodes(
            self, index: NodeSamplerInput
    ) -> Union[SamplerOutput, HeteroSamplerOutput]:
        r"""Implements neighbor sampling by calling 'torch-sparse' sampling
        routines, conditional on the type of data object."""

        if isinstance(index, (list, tuple)):
            index = torch.tensor(index)

        # Tuple[FeatureStore, GraphStore] currently only supports heterogeneous
        # sampling:
        if self.data_cls == 'custom' or issubclass(self.data_cls, HeteroData):
            node, row, col, edge = self._hetero_sparse_neighbor_sample(
                {self.input_type: index})

            # Convert back from edge type strings to PyG EdgeType, as required
            # by SamplerOutput:
            return HeteroSamplerOutput(
                metadata=index.numel(),
                node=node,
                row=remap_keys(row, self.to_edge_type),
                col=remap_keys(col, self.to_edge_type),
                edge=remap_keys(edge, self.to_edge_type),
            )
        elif issubclass(self.data_cls, Data):
            node, row, col, edge = self._sparse_neighbor_sample(index)
            return SamplerOutput(
                metadata=index.numel(),
                node=node,
                row=row,
                col=col,
                edge=edge,
            )
        else:
            raise TypeError(f'{self.__class__.__name__} found invalid type: '
                            f'{type(self.data_cls)}')

    # Edge-based sampling #####################################################

    def sample_from_edges(
        self,
        index: EdgeSamplerInput,
        *args,
        **kwargs,
    ) -> SamplerOutput:
        negative_sampling_ratio = kwargs.get('negative_sampling_ratio', 0.0)
        query = [torch.stack(s, dim=0) for s in zip(*index)]
        edge_label_index = torch.stack(query[:2], dim=0)
        edge_label = query[2]
        edge_label_time = query[3] if len(query) == 4 else None

        out = add_negative_samples(edge_label_index, edge_label,
                                   edge_label_time, self.num_src_nodes,
                                   self.num_dst_nodes, negative_sampling_ratio)
        edge_label_index, edge_label, edge_label_time = out

        orig_edge_label_index = edge_label_index
        if (self.data_cls == 'custom'
                or issubclass(self.data_cls, HeteroData)):
            if self.input_type[0] != self.input_type[-1]:
                query_src = edge_label_index[0]
                query_src, reverse_src = query_src.unique(return_inverse=True)
                query_dst = edge_label_index[1]
                query_dst, reverse_dst = query_dst.unique(return_inverse=True)
                edge_label_index = torch.stack([reverse_src, reverse_dst], 0)
                query_node_dict = {
                    self.input_type[0]: query_src,
                    self.input_type[-1]: query_dst,
                }
            else:  # Merge both source and destination node indices:
                query_nodes = edge_label_index.view(-1)
                query_nodes, reverse = query_nodes.unique(return_inverse=True)
                edge_label_index = reverse.view(2, -1)
                query_node_dict = {self.input_type[0]: query_nodes}

            node_time_dict = self.node_time_dict
            if edge_label_time is not None:
                node_time_dict = set_node_time_dict(
                    node_time_dict, self.input_type, orig_edge_label_index,
                    edge_label_time, self.num_src_nodes, self.num_dst_nodes)

            out = self._hetero_sparse_neighbor_sample(
                query_node_dict, node_time_dict=node_time_dict)

            node, row, col, edge = out
            return HeteroSamplerOutput(
                node=node,
                row=remap_keys(row, self.to_edge_type),
                col=remap_keys(col, self.to_edge_type),
                edge=remap_keys(edge, self.to_edge_type),
                metadata=(edge_label_index, edge_label, edge_label_time),
            )

        elif issubclass(self.data_cls, Data):
            query_nodes = edge_label_index.view(-1)
            query_nodes, reverse = query_nodes.unique(return_inverse=True)
            edge_label_index = reverse.view(2, -1)

            node, row, col, edge = self._sparse_neighbor_sample(query_nodes)
            return SamplerOutput(
                node=node,
                row=row,
                col=col,
                edge=edge,
                metadata=(edge_label_index, edge_label),
            )


###############################################################################


def remap_keys(original: Dict, mapping: Dict) -> Dict:
    return {mapping[k]: v for k, v in original.items()}