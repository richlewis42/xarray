# TODO: replace aggregate and iterator methods by a 'groupby' method/object
# like pandas
import functools
import re
from collections import OrderedDict

import numpy as np
import pandas as pd

import xarray
import dataset as dataset_
import groupby
import ops
from common import AbstractArray
from utils import expanded_indexer, FrozenOrderedDict, remap_loc_indexers


class _LocIndexer(object):
    def __init__(self, ds_array):
        self.ds_array = ds_array

    def _remap_key(self, key):
        indexers = remap_loc_indexers(self.ds_array.dataset.variables,
                                      self.ds_array._key_to_indexers(key))
        return tuple(indexers.values())

    def __getitem__(self, key):
        return self.ds_array[self._remap_key(key)]

    def __setitem__(self, key, value):
        self.ds_array[self._remap_key(key)] = value


class DatasetArray(AbstractArray):
    """Hybrid between Dataset and Array.

    Dataset arrays are the primary way to do computations with Dataset
    variables. They are designed to make it easy to manipulate arrays in the
    context of an intact Dataset object. Indeed, the contents of a DatasetArray
    are uniquely defined by its `dataset` and `focus` paramters.

    Getting items from or doing mathematical operations with a dataset array
    returns another dataset array.

    The design of DatasetArray is strongly inspired by the Iris Cube. However,
    dataset arrays are much lighter weight than cubes. They are simply aligned,
    labeled datasets and do not explicitly guarantee or rely on the CF model.
    """
    def __init__(self, dataset, focus):
        """
        Parameters
        ----------
        dataset : xray.Dataset
            The dataset on which to build this dataset array.
        focus : str
            The name of the "focus variable" in `dataset` on which this object
            is oriented. This is the variable on which mathematical operations
            are applied.
        """
        if not isinstance(dataset, dataset_.Dataset):
            dataset = dataset_.Dataset(dataset)
        if focus not in dataset and focus not in dataset.virtual_variables:
            raise ValueError('focus %r is not a variable in dataset %r'
                             % (focus, dataset))
        self.dataset = dataset
        self.focus = focus

    @property
    def array(self):
        return self.dataset.variables[self.focus]
    @array.setter
    def array(self, value):
        self.dataset[self.focus] = value

    # _data is necessary for AbstractArray
    @property
    def _data(self):
        return self.array._data

    @property
    def data(self):
        """The array's data as a numpy.ndarray"""
        return self.array.data
    @data.setter
    def data(self, value):
        self.array.data = value

    @property
    def dimensions(self):
        return self.array.dimensions

    def _key_to_indexers(self, key):
        return OrderedDict(
            zip(self.dimensions, expanded_indexer(key, self.ndim)))

    def __getitem__(self, key):
        if isinstance(key, basestring):
            # grab another dataset array from the dataset
            return self.dataset[key]
        else:
            # orthogonal array indexing
            return self.indexed_by(**self._key_to_indexers(key))

    def __setitem__(self, key, value):
        if isinstance(key, basestring):
            # add an array to the dataset
            self.dataset[key] = value
        else:
            # orthogonal array indexing
            self.array[key] = value

    def __delitem__(self, key):
        del self.dataset[key]

    def __contains__(self, key):
        return key in self.dataset

    @property
    def loc(self):
        """Attribute for location based indexing like pandas.
        """
        return _LocIndexer(self)

    def __iter__(self):
        for n in range(len(self)):
            yield self[n]

    @property
    def attributes(self):
        return self.array.attributes

    @property
    def encoding(self):
        return self.array.encoding

    @property
    def variables(self):
        return self.dataset.variables

    @property
    def coordinates(self):
        return FrozenOrderedDict((k, self.dataset.variables[k])
                                 for k in self.dimensions)

    def copy(self):
        return self.__copy__()

    def __copy__(self):
        # shallow copy the underlying dataset
        return DatasetArray(self.dataset.copy(), self.focus)

    # mutable objects should not be hashable
    __hash__ = None

    def __str__(self):
        #TODO: make this less hacky
        return re.sub(' {4}(%s\s+%s)' % (self.dtype, self.focus),
                      r'--> \1', str(self.dataset))

    def __repr__(self):
        if self.ndim > 0:
            dim_summary = ', '.join('%s: %s' % (k, v) for k, v
                                    in zip(self.dimensions, self.shape))
            contents = ' (%s): %s' % (dim_summary, self.dtype)
        else:
            contents = ': %s' % self.data
        return '<xray.%s %r%s>' % (type(self).__name__, self.focus, contents)

    def indexed_by(self, **indexers):
        """Return a new dataset array whose dataset is given by indexing along
        the specified dimension(s).

        See Also
        --------
        Dataset.indexed_by
        """
        ds = self.dataset.indexed_by(**indexers)
        if self.focus not in ds:
            # always keep focus variable in the dataset, even if it was
            # unselected because indexing made it a scaler
            ds[self.focus] = self.array.indexed_by(**indexers)
        return type(self)(ds, self.focus)

    def labeled_by(self, **indexers):
        """Return a new dataset array whose dataset is given by selecting
        coordinate labels along the specified dimension(s).

        See Also
        --------
        Dataset.labeled_by
        """
        return self.indexed_by(**remap_loc_indexers(self.dataset.variables,
                                                    indexers))

    def renamed(self, new_name):
        """Returns a new DatasetArray with this DatasetArray's focus variable
        renamed.
        """
        renamed_dataset = self.dataset.renamed({self.focus: new_name})
        return type(self)(renamed_dataset, new_name)

    def select(self, *names):
        """Returns a new DatasetArray with only the named variables, as well
        as this DatasetArray's focus.

        See Also
        --------
        Dataset.select
        """
        names = names + (self.focus,)
        return type(self)(self.dataset.select(*names), self.focus)

    def unselected(self):
        """Returns a copy of this DatasetArray's dataset with this
        DatasetArray's focus variable removed.
        """
        return self.dataset.unselect(self.focus)

    def unselect(self, *names):
        """Returns a new DatasetArray without the named variables.

        See Also
        --------
        Dataset.unselect
        """
        if self.focus in names:
            raise ValueError('cannot unselect the focus variable of a '
                             'DatasetArray with unselect. Use the `unselected`'
                             'method or the `unselect` method of the dataset.')
        return type(self)(self.dataset.unselect(*names), self.focus)

    def refocus(self, new_var, name=None):
        """Returns a copy of this DatasetArray's dataset with this
        DatasetArray's focus variable replaced by `new_var`.

        If `new_var` is a dataset array, its contents will be merged in.
        """
        if not hasattr(new_var, 'dimensions'):
            new_var = type(self.array)(self.array.dimensions, new_var)
        if self.focus not in self.dimensions:
            # only unselect the focus from the dataset if it isn't a coordinate
            # variable
            ds = self.unselected()
        else:
            ds = self.dataset
        if name is None:
            name = self.focus + '_'
        ds[name] = new_var
        return type(self)(ds, name)

    def groupby(self, group, squeeze=True):
        """Group this dataset by unique values of the indicated group.

        Parameters
        ----------
        group : str or DatasetArray
            Array whose unique values should be used to group this array. If a
            string, must be the name of a variable contained in this dataset.
        squeeze : boolean, optional
            If "group" is a coordinate of this array, `squeeze` controls
            whether the subarrays have a dimension of length 1 along that
            coordinate or if the dimension is squeezed out.

        Returns
        -------
        grouped : GroupBy
            A `GroupBy` object patterned after `pandas.GroupBy` that can be
            iterated over in the form of `(unique_value, grouped_array)` pairs
            or over which grouped operations can be applied with the `apply`
            and `reduce` methods (and the associated aliases `mean`, `sum`,
            `std`, etc.).
        """
        if isinstance(group, basestring):
            group = self[group]
        return groupby.ArrayGroupBy(self, group.focus, group, squeeze=squeeze)

    def transpose(self, *dimensions):
        """Return a new DatasetArray object with transposed dimensions.

        Note: Although this operation returns a view of this array's data, it
        is not lazy -- the data will be fully loaded.

        Parameters
        ----------
        *dimensions : str, optional
            By default, reverse the dimensions. Otherwise, reorder the
            dimensions to this order.

        Returns
        -------
        transposed : DatasetArray
            The returned DatasetArray's array is transposed.

        Notes
        -----
        Although this operation returns a view of this array's data, it is
        not lazy -- the data will be fully loaded.

        See Also
        --------
        numpy.transpose
        Array.transpose
        """
        return self.refocus(self.array.transpose(*dimensions), self.focus)

    def squeeze(self, dimension=None):
        """Return a new DatasetArray object with squeezed data.

        Parameters
        ----------
        dimensions : None or str or tuple of str, optional
            Selects a subset of the length one dimensions. If a dimension is
            selected with length greater than one, an error is raised. If
            None, all length one dimensions are squeezed.

        Returns
        -------
        squeezed : DatasetArray
            This array, but with with all or a subset of the dimensions of
            length 1 removed.

        Notes
        -----
        Although this operation returns a view of this array's data, it is
        not lazy -- the data will be fully loaded.

        See Also
        --------
        numpy.squeeze
        """
        return type(self)(self.dataset.squeeze(dimension), self.focus)

    def reduce(self, func, dimension=None, axis=None, **kwargs):
        """Reduce this array by applying `func` along some dimension(s).

        Parameters
        ----------
        func : function
            Function which can be called in the form
            `f(x, axis=axis, **kwargs)` to return the result of reducing an
            np.ndarray over an integer valued axis.
        dimension : str or sequence of str, optional
            Dimension(s) over which to repeatedly apply `func`.
        axis : int or sequence of int, optional
            Axis(es) over which to repeatedly apply `func`. Only one of the
            'dimension' and 'axis' arguments can be supplied. If neither are
            supplied, then the reduction is calculated over the flattened array
            (by calling `f(x)` without an axis argument).
        **kwargs : dict
            Additional keyword arguments passed on to `func`.

        Notes
        -----
        If `reduce` is called with multiple dimensions (or axes, which
        are converted into dimensions), then the reduce operation is
        performed repeatedly along each dimension in turn from left to right.

        Returns
        -------
        reduced : DatasetArray
            DatasetArray with this object's array replaced with an array with
            summarized data and the indicated dimension(s) removed.
        """
        var = self.array.reduce(func, dimension, axis, **kwargs)
        drop = set(self.dimensions) - set(var.dimensions)
        # For now, take an aggressive strategy of removing all variables
        # associated with any dropped dimensions
        # TODO: save some summary (mean? bounds?) of dropped variables
        drop |= {k for k, v in self.dataset.variables.iteritems()
                 if any(dim in drop for dim in v.dimensions)}
        ds = self.dataset.unselect(*drop)
        ds[self.focus] = var
        return type(self)(ds, self.focus)

    @classmethod
    def from_stack(cls, arrays, dimension='stacked_dimension',
                   stacked_indexers=None, length=None, template=None):
        """Stack arrays along a new or existing dimension to form a new
        DatasetArray.

        Parameters
        ----------
        arrays : iterable of Array
            Arrays to stack together. Each variable is expected to have
            matching dimensions and shape except for along the stacked
            dimension.
        dimension : str or Array, optional
            Name of the dimension to stack along. This can either be a new
            dimension name, in which case it is added along axis=0, or an
            existing dimension name, in which case the location of the
            dimension is unchanged. Where to insert the new dimension is
            determined by whether it is found in the first array. If dimension
            is provided as an XArray or DatasetArray, the focus of the dataset
            array is used as the stacking dimension and the array is added to
            the returned dataset.
        stacked_indexers : iterable of indexers, optional
            Iterable of indexers of the same length as variables which
            specifies how to assign variables along the given dimension. If
            not supplied, stacked_indexers is inferred from the length of each
            variable along the dimension, and the variables are stacked in the
            given order.
        length : int, optional
            Length of the new dimension. This is used to allocate the new data
            array for the stacked variable data before iterating over all
            items, which is thus more memory efficient and a bit faster. If
            dimension is provided as an array, length is calculated
            automatically.
        template : DatasetArray, optional
            This option is used internally to speed-up groupby operations. The
            template's attributes and variables that are not along the stacked
            dimension are added to the returned array. Furthermore, if a
            template is given, some checks of internal consistency between
            arrays to stack are skipped.

        Returns
        -------
        stacked : DatasetArray
            Stacked dataset array formed by stacking all the supplied variables
            along the new dimension.
        """
        def unselect_nonfocus_dims(dataset_array):
            # Given a dataset array, unselect all dimensions found in the
            # dataset that aren't also found in the dimensions of the array.
            # Returns either a modified copy or the original dataset array if
            # there were no dimensions to remove.
            other_dims = [k for k in dataset_array.dataset.dimensions
                          if k not in dataset_array.dimensions]
            if other_dims:
                dataset_array = dataset_array.unselect(*other_dims)
            return dataset_array

        ds = dataset_.Dataset()
        if isinstance(dimension, basestring):
            dim_name = dimension
        else:
            dim_name, = dimension.dimensions
            if isinstance(dimension, cls):
                ds[dimension.focus] = unselect_nonfocus_dims(dimension)

        if template is not None:
            # use metadata from the template dataset array
            focus = template.focus
            old_dim_name, = template[dim_name].dimensions
            ds.merge(template.dataset.unselect(old_dim_name), inplace=True)
        else:
            # figure out metadata by inspecting each array
            focus = 'stacked_variable'
            arrays = list(arrays)
            for array in arrays:
                if isinstance(array, cls):
                    drop = [array.focus] + [k for k in array.dataset.dimensions
                                            if k == dim_name]
                    ds.merge(array.dataset.unselect(*drop), inplace=True)
                    if focus is 'stacked_variable':
                        focus = array.focus

        ds[focus] = xarray.XArray.from_stack(arrays, dimension,
                                             stacked_indexers, length,
                                             template)
        return unselect_nonfocus_dims(cls(ds, focus))

    def to_dataframe(self):
        """Convert this array into a pandas.DataFrame.

        Non-coordinate variables in this array's dataset (which include the
        view's data) form the columns of the DataFrame. The DataFrame is be
        indexed by the Cartesian product of the dataset's coordinates.
        """
        return self.dataset.to_dataframe()

    def to_series(self):
        """Conver this array into a pandas.Series.

        The Series is be indexed by the Cartesian product of the coordinates.
        Unlike `to_dataframe`, only the variable at the focus of this array is
        including in the returned series.
        """
        # np.asarray is necessary to work around a pandas bug:
        # https://github.com/pydata/pandas/issues/6439
        coords = [np.asarray(v) for v in self.coordinates.values()]
        index_names = self.coordinates.keys()
        index = pd.MultiIndex.from_product(coords, names=index_names)
        return pd.Series(self.data.reshape(-1), index=index, name=self.focus)

    def __array_wrap__(self, obj, context=None):
        return self.refocus(self.array.__array_wrap__(obj, context))

    @staticmethod
    def _unary_op(f):
        @functools.wraps(f)
        def func(self, *args, **kwargs):
            return self.refocus(f(self.array, *args, **kwargs),
                                self.focus + '_' + f.__name__)
        return func

    def _check_coordinates_compat(self, other):
        # TODO: possibly automatically select index intersection instead?
        if hasattr(other, 'coordinates'):
            for k, v in self.coordinates.iteritems():
                if (k in other.coordinates
                        and not np.array_equal(v, other.coordinates[k])):
                    raise ValueError('coordinate %r is not aligned' % k)

    @staticmethod
    def _binary_op(f, reflexive=False):
        @functools.wraps(f)
        def func(self, other):
            # TODO: automatically group by other variable dimensions to allow
            # for broadcasting dimensions like 'dayofyear' against 'time'
            self._check_coordinates_compat(other)
            ds = self.unselected()
            if hasattr(other, 'unselected'):
                ds.merge(other.unselected(), inplace=True)
            other_array = getattr(other, 'array', other)
            other_focus = getattr(other, 'focus', 'other')
            focus = self.focus + '_' + f.__name__ + '_' + other_focus
            ds[focus] = (f(self.array, other_array)
                         if not reflexive
                         else f(other_array, self.array))
            return type(self)(ds, focus)
        return func

    @staticmethod
    def _inplace_binary_op(f):
        @functools.wraps(f)
        def func(self, other):
            self._check_coordinates_compat(other)
            other_array = getattr(other, 'array', other)
            self.array = f(self.array, other_array)
            if hasattr(other, 'unselected'):
                self.dataset.merge(other.unselected(), inplace=True)
            return self
        return func

ops.inject_special_operations(DatasetArray, priority=60)


def align(array1, array2):
    """Given two Dataset or DatasetArray objects, returns two new objects where
    all coordinates found on both datasets are replaced by their intersection,
    and thus are aligned for performing mathematical operations.
    """
    # TODO: automatically align when doing math with arrays, or better yet
    # calculate the union of the indices and fill in the mis-aligned data with
    # NaN.
    overlapping_coords = {k: (array1.coordinates[k].data
                              & array2.coordinates[k].data)
                          for k in array1.coordinates
                          if k in array2.coordinates}
    return tuple(ar.labeled_by(**overlapping_coords)
                 for ar in [array1, array2])
