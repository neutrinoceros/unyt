"""
a dask array class (unyt_dask_array) and helper functions for unyt.



"""

from dask.array.core import Array, finalize, is_valid_array_chunk  # TO DO: handle optional dep.

import unyt.array as ua


unyt_quantity = ua.unyt_quantity
unyt_array = ua.unyt_array


# funcs which we can apply to the factor separately from dask
_apply_func_to_unyt = [ua._multiply_units, ua._divide_units, ua._power_unit, ua._sqrt_unit, ua._square_unit]

# funcs for which we need to apply factor before dask operations (add, logical, exp)
_apply_fac_before_call = [ua._return_without_unit, ua._preserve_units, ua._comparison_unit, ua._bitop_units]
_exceptions_that_should_be_applied = 'clip'

# funcs for which the factor does not change
_pass_through_fac = [ua._passthrough_unit]
_exceptions_that_should_be_passthrough = ["maximum", "minimum"]

ua_inst = unyt_array([1.], "")

_apply_func_to_unyt = ['matmul', 'power', 'sqrt', 'square', 'multiply', 'divide', 'true_divide', 'reciprocal' ]
force_apply = ["floor", "ceil", "trunc", "spacing"]  # unyt treats as passthrough, unyt_dask must preseve
call_without_fac = ['sign', 'isreal', "isinf", "isnan", "isfinite", "iscomplex"]  # unyt treats as _return_without_unit, but we don't need to apply fac
what_to_do = {}
for ufunc, op in ua_inst._ufunc_registry.items():
    fname = ufunc.__name__
    if op == ua._passthrough_unit and fname not in force_apply:
        # does nothing to the factor or unit
        what_to_do[ufunc] = "pass_through_fac"
    elif fname in _apply_func_to_unyt or fname in force_apply:
        # these will apply the function to the factors
        what_to_do[ufunc] = "apply_func_to_unyt"
    elif fname in call_without_fac:
        what_to_do[ufunc] = "call_without_fac"
    else:
        # need to apply factors prior to dask calls (_preserve_unit, logical operations, add, subtract)
        what_to_do[ufunc] = "apply_fac_before_dask"



_use_simple_decorator = [
    "min",
    "max",
    "argmin",
    "argmax",
    "sum",
    "trace",
    "mean",
    "std",
    "cumsum",
    "squeeze",
    "rechunk",
    "clip",
    "view",
    "swapaxes",
    "round",
    "copy",
    "__deepcopy__",
    "repeat",
    "astype",
    "reshape",
    "topk",
]


def _simple_dask_decorator(dask_func, current_unyt_dask):
    """
    a decorator for the simpler dask functions that can just copy over the current
    unit info and unit conversion factor after applying the dask function. this
    includes functions that return single values (e.g., min()) or functions that
    change the array in ways that do not affect units like reshaping or rounding.

    Parameters
    ----------

    dask_func: func handle
       the dask function handle to call
    current_unyt_dask: unyt_dask_array
       the current instance of a unyt_dask_array

    Returns a new unyt_dask_array instance with appropriate units and factor
    """
    def wrapper(*args, **kwargs):
        da = dask_func(*args, **kwargs)  # will return standard dask array
        un_qua = current_unyt_dask._unyt_quantity
        factor = current_unyt_dask.factor
        return _attach_unyt_quantity(da, un_qua, factor)

    return wrapper


_unyt_funcs_to_track = [
    "to",
    "in_units",
    "in_cgs",
    "in_base",
    "in_mks"
]


def _track_factor(unyt_func_name, current_unyt_dask):
    """
    a decorator to use with unyt functions to track the conversion factor

    Parameters
    ----------

    unyt_func_name: str
        the name of the function to call from the sidecar _unyt_quantity. Must be
        an attribute of unyt.unyt_quantity.
    current_unyt_dask: unyt_dask_array
        the current instance of a unyt_dask_array


    Returns a new unyt_dask_array instance with appropriate units and factor
    """
    def wrapper(*args, **kwargs):

        # current value of sidecar quantity
        init_val = current_unyt_dask._unyt_quantity.value

        # get the unyt function handle and call it
        the_func = getattr(current_unyt_dask._unyt_quantity, unyt_func_name)
        unyt_quantity = the_func(*args, **kwargs)

        # calculate the cumulative conversion factor and pull out the new name and units
        factor = current_unyt_dask.factor * unyt_quantity.value / init_val
        unyt_name = unyt_quantity.name
        units = unyt_quantity.units

        # create a new unyt dask object and set the unit state with our new
        # converted factor, return the new object
        new_obj = unyt_from_dask(current_unyt_dask)
        new_obj._set_unit_state(units, unyt_quantity, unyt_name, factor)

        return new_obj
    return wrapper



class unyt_dask_array(Array):
    """
    a dask.array.core.Array subclass that tracks units. Easiest to use the
    unyt_from_dask helper function to generate new instances.

    Parameters
    ----------

    All parameters are those for dask.array.core.Array

    """

    def __new__(
        clss,
        dask_graph,
        name,
        chunks,
        dtype=None,
        meta=None,
        shape=None,
        units=None,
        registry=None,
        bypass_validation=False,
        input_units=None,
        unyt_name=None,
    ):

        # get the base dask array
        obj = super(unyt_dask_array, clss).__new__(
            clss,
            dask_graph,
            name,
            chunks,
            dtype,
            meta,
            shape,
        )

        # attach our unyt sidecar quantity
        dtype = obj.dtype
        obj._unyt_quantity = unyt_quantity(
            1.0,
            units,
            registry,
            dtype,
            bypass_validation,
            input_units,
            unyt_name,
        )

        obj.units = obj._unyt_quantity.units
        obj.unyt_name = obj._unyt_quantity.name
        obj.factor = 1.0  # dtype issue here?

        return obj

    def _elemwise(self, ufunc, *args, **kwargs):
        """
        _elemwise is a dask.array hook for catching daskified np ufuncs. This
        attempts to apply the given ufunc to the sidecar unyt_quantity. If
        successful, this function will return another unyt_dask_array. If
        unsuccessful, will return a standard dask array.

        *args comes in here with dask objects.
        args[0] is the current dask graph. args[1] might be another if this is a
        binary operation? ignore that case for now...
        """
        b = self._track_unyt_ops(ufunc, None, *args, **kwargs)

        # NEED TO CHECK IF THIS IS A BINARY OPERATION AND HANDLE OTHER DASK
        # OBJECTS IF NECESSARY. DO THIS HERE. should be able to use existing
        # unyt stuff to check.

        # apply the ufunc to the unyt_quantity first to see if it is compatible
        # with units. Maybe put this in a try block...
        un_quan = ufunc(self._unyt_quantity, *args[1:], **kwargs)

        if hasattr(un_quan, 'units'):
            # operation results in a quantity with units, so track the factor,
            # get the raw dask result and return a new dask_unyt_array
            factor = ufunc(self.factor, *args[1:], **kwargs)
            dask_result = super()._elemwise(ufunc, *args, **kwargs)
            return _attach_unyt_quantity(dask_result, un_quan, factor)
        else:
            # the operation results in a quantity without units, apply the factor
            # and then call the ufunc!
            new_self, _ = self._apply_factors_to_graphs()
            new_args = (new_self, ) + args[1:] # replace the dask arg with the new
            return super()._elemwise(ufunc, *new_args, **kwargs)

    def to_dask(self):
        """ return a plain dask array. Only copies high level graphs, should be cheap...
        """
        (cls, args) = self.__reduce__()
        return super().__new__(Array, *args)

    def __array_ufunc__(self, numpy_ufunc, method, *inputs, **kwargs):
        # this will catch np.sqrt but not dask.array.sqrt

        b = self._track_unyt_ops(numpy_ufunc, method, *inputs, **kwargs)

        # need to hand plain dask arrays to super()...
        clean_inputs = []
        for input in inputs:
            if isinstance(input, unyt_dask_array):
                clean_inputs.append(input.to_dask())
            else:
                clean_inputs.append(input)
        dask_result = super().__array_ufunc__(numpy_ufunc, method, *clean_inputs, **kwargs)

        # apply the ufunc to the unyt_quantity first to see if it is compatible
        # with units. Maybe put this in a try block...
        un_quan = numpy_ufunc(self._unyt_quantity, *clean_inputs[1:], **kwargs)


        return dask_result

    def _track_unyt_ops(self, func, method, *inputs, **kwargs):


        print("hello")
        here_from_elemwise = method is None

        is_unyt_da = [isinstance(i, unyt_dask_array) for i in inputs]
        is_binary = func in binary_operators
        is_unary = func in unary_operators

        if is_unary:
            print("hello sdfgdf")
            # apply the ufunc to the unyt_quantity first to see if it is compatible
            # with units. Maybe put this in a try block...
            un_quan = func(self._unyt_quantity, *inputs[1:], **kwargs)
            if hasattr(un_quan, 'units'):
                # operation results in a quantity with units, so apply operation to factor
                factor = func(self.factor, *inputs[1:], **kwargs)

        elif is_binary:
            if is_unyt_da[1]:
                # for now, ALWAYS apply factor to each
                new_self, new_other = self._apply_factors_to_graphs(inputs[1])
            else:
                raise ValueError("expected unyt_dask_array as argument")




                other_uq = inputs[1]._unyt_quantity
                un_quan = func(self._unyt_quantity, other_uq, *inputs[2:], **kwargs)

            if hasattr(un_quan, 'units'):
                # operation results in a quantity with units, so apply operation to factor
                factor = func(self.factor, *inputs[1:], **kwargs)
            else:
                # the operation results in a quantity without units, apply the factor
                # and then call the ufunc!
                new_self, _ = self._apply_factors_to_graphs()
                new_args = (new_self,) + args[1:]  # replace the dask arg with the new




        f = 1
        # # a copy of the inputs with unyt_quantities
        # unyt_dask_indices = [inum for inum, i in enumerate(inputs) if isinstance(i, unyt_dask_array)]
        #
        # # determine if we need to multiply unyt factors before calling the dask
        # # function
        # unary_operators
        #
        #
        #
        #
        # unyt_input = []
        # clean_inputs = []
        # for inp in input:
        #     if isinstance(inp, unyt_dask_array):
        #         unyt_input.append(inp._unyt_quantity)

    def __getattribute__(self, name):

        if name in _unyt_funcs_to_track:
            return _track_factor(name, self)

        result = super().__getattribute__(name)
        if name in _use_simple_decorator:
            return _simple_dask_decorator(result, self)

        return result

    def __dask_postcompute__(self):
        # a dask hook to catch after .compute(), see
        # https://docs.dask.org/en/latest/custom-collections.html#example-dask-collection
        return _finalize_unyt, ((self.units, self.factor))

    # straightforward operations where the operation applies to the unit
    # These methods bypass __getattribute__, but maybe there's a way
    # to more programmatically generate these (e.g., see
    # https://stackoverflow.com/a/57211141/9357244)
    def __abs__(self):
        return _attach_unyt_quantity(super().__abs__(), self._unyt_quantity, self.factor)

    def __pow__(self, other):
        # unyt and dask implement this directly:
        un_qua = self._unyt_quantity.__pow__(other)
        factor = self.factor ** other
        return _attach_unyt_quantity(super().__pow__(other), un_qua, factor)

    def __mul__(self, other):
        if isinstance(other, unyt_dask_array):
            factor = self.factor * other.factor
            un_qua = self._unyt_quantity * other._unyt_quantity
        else:
            factor = self.factor
            un_qua = self._unyt_quantity

        return _attach_unyt_quantity(super().__mul__(other), un_qua, factor)

    def __rmul__(self, other):
        if isinstance(other, unyt_dask_array):
            factor = self.factor * other.factor
            un_qua = self._unyt_quantity * other._unyt_quantity
        else:
            factor = self.factor
            un_qua = self._unyt_quantity

        return _attach_unyt_quantity(super().__rmul__(other), un_qua, factor)

    def __div__(self, other):
        if isinstance(other, unyt_dask_array):
            factor = self.factor / other.factor
            un_qua = self._unyt_quantity / other._unyt_quantity
        else:
            factor = self.factor
            un_qua = self._unyt_quantity
        return _attach_unyt_quantity(super().__div__(other), un_qua, factor)

    def __rdiv__(self, other):
        if isinstance(other, unyt_dask_array):
            factor = other.factor / self.factor
            un_qua = other._unyt_quantity / self._unyt_quantity
        else:
            factor = self.factor
            un_qua = self._unyt_quantity
        return _attach_unyt_quantity(super().__rdiv__(other), un_qua, factor)

    def __truediv__(self, other):
        if isinstance(other, unyt_dask_array):
            factor = self.factor / other.factor
            un_qua = self._unyt_quantity / other._unyt_quantity
        else:
            factor = self.factor
            un_qua = self._unyt_quantity
        return _attach_unyt_quantity(super().__truediv__(other), un_qua, factor)

    # operations involving other arrays (TO DO: use the unyt_array classes
    # better to do all the unit checks...)

    def _apply_factors_to_graphs(self, other=None):
        # applies the factors to the dask graphs of each dask array, returns
        # new unyt_dask arrays for each. Used when two incoming unyt_dask arrays
        # have the same units but different factors from prior conversions.
        new_self = unyt_from_dask(super().__mul__(self.factor), self.units)
        new_other = None
        if other is not None:
            new_other = unyt_from_dask(other * other.factor, self.units)
        return new_self, new_other

    def __add__(self, other):
        if self.units == other.units and self.factor != other.factor:
            # same units, but there was a previous conversion
            new_self, new_other = self._apply_factors_to_graphs(other)
            return unyt_from_dask(new_self.__add__(new_other), new_self.units)
        elif self.units != other.units:
            raise ValueError("unyt_dask arrays must have the same units to add")
        else:
            # same units and same factor, the factor can be applied on compute
            return _attach_unyt_quantity(super().__add__(other), self._unyt_quantity, self.factor)

    def __sub__(self, other):
        if self.units == other.units and self.factor != other.factor:
            # same units, but there was a previous conversion
            new_self, new_other = self._apply_factors_to_graphs(other)
            return unyt_from_dask(new_self.__sub__(new_other), new_self.units)
        elif self.units != other.units:
            raise ValueError("unyt_dask arrays must have the same units to subtract")
        else:
            # same units and same factor, the factor can be applied on compute
            return _attach_unyt_quantity(super().__sub__(other), self._unyt_quantity, self.factor)

    def _get_unit_state(self):
        # returns the unit state of the object
        return self.units, self._unyt_quantity, self.unyt_name, self.factor

    def _set_unit_state(self, units, unyt_quantity, unyt_name, factor):
        # sets just the unit state of the object
        self.units = units
        self._unyt_quantity = unyt_quantity
        self.unyt_name = unyt_name
        self.factor = factor


def _finalize_unyt(results, unit_name, factor):
    """
    the function to call from the __dask_postcompute__ hook.

    Parameters
    ----------
    results : the dask results object
    unit_name : the units of the result
    factor : the current value of the unit conversion factor

    Returns
    -------
    unyt_array

    """

    # here, we first call the standard finalize function for a dask array
    # and then return a standard unyt_array from the now in-memory result.
    return unyt_array(finalize(results) * factor, unit_name)

def _attach_unyt_quantity(dask_array, unyt_quantity, factor=None):
    """
    this function instantiates a new unyt_dask_array instance and then sets
    the unit state, including units and conversion factor. Used to wrap
    dask operations and track the cumulative conversion factor.

    Parameters
    ----------
    dask_array : a standard dask array
    unyt_quantity : a standard unity quantity
    factor : float
        the cumulative conversion factor, default to 1.0
    remaining arguments get passed to unyt.unyt_array, check there for a
    description.
    """
    # first create our new instance:
    out = unyt_from_dask(dask_array)

    # attach the unyt_quantity
    units = unyt_quantity.units
    unyt_name = unyt_quantity.name
    if factor is None:
        factor = 1.

    out._set_unit_state(units, unyt_quantity, unyt_name, factor)
    return out


def unyt_from_dask(
    dask_array,
    units=None,
    registry=None,
    bypass_validation=False,
    unyt_name=None,
):
    """
    creates a unyt_dask_array from a standard dask array.

    Parameters
    ----------
    dask_array : a standard dask array

    remaining arguments get passed to unyt.unyt_array, check there for a
    description.

    Examples
    --------

    >>> from unyt import dask_array
    >>> import dask.array as da
    >>> x = da.random.random((10000, 10000), chunks=(1000, 1000))
    >>> x_da = dask_array.unyt_from_dask(x, 'm')
    >>> x_da.units
    m
    >>> x_da.mean().units()
    m
    >>> x_da.mean().compute()
    unyt_array(0.50001502, 'm')
    >>> x_da.to('cm').mean().compute()
    unyt_array(50.00150242, 'cm')
    >>> (x_da.to('cm')**2).mean().compute()
    unyt_array(3333.37805754, 'cm**2')

    """

    # reduce the dask array to pull out the arguments required for instantiating
    # a new dask.array.core.Array object and then initialize our new unyt_dask
    # array
    (cls, args) = dask_array.__reduce__()

    da = unyt_dask_array(
        *args,
        units=units,
        registry=registry,
        bypass_validation=bypass_validation,
        unyt_name=unyt_name
    )

    return da
