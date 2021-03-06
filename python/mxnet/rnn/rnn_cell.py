# coding: utf-8
# pylint: disable=no-member, invalid-name, protected-access, no-self-use
# pylint: disable=too-many-branches, too-many-arguments, no-self-use
# pylint: disable=too-many-lines
"""Definition of various recurrent neural network cells."""
from __future__ import print_function

import warnings

from .. import symbol, init, ndarray, _symbol_internal
from ..base import string_types, numeric_types


def _cells_state_shape(cells):
    return sum([c.state_shape for c in cells], [])

def _cells_state_info(cells):
    return sum([c.state_info for c in cells], [])

def _cells_begin_state(cells, **kwargs):
    return sum([c.begin_state(**kwargs) for c in cells], [])

def _cells_unpack_weights(cells, args):
    for cell in cells:
        args = cell.unpack_weights(args)
    return args

def _cells_pack_weights(cells, args):
    for cell in cells:
        args = cell.pack_weights(args)
    return args

def _normalize_sequence(length, inputs, layout, merge, in_layout=None):
    assert inputs is not None, \
        "unroll(inputs=None) has been deprecated. " \
        "Please create input variables outside unroll."

    axis = layout.find('T')
    in_axis = in_layout.find('T') if in_layout is not None else axis
    if isinstance(inputs, symbol.Symbol):
        if merge is False:
            assert len(inputs.list_outputs()) == 1, \
                "unroll doesn't allow grouped symbol as input. Please convert " \
                "to list with list(inputs) first or let unroll handle splitting."
            inputs = list(symbol.split(inputs, axis=in_axis, num_outputs=length,
                                       squeeze_axis=1))
    else:
        assert length is None or len(inputs) == length
        if merge is True:
            inputs = [symbol.expand_dims(i, axis=axis) for i in inputs]
            inputs = symbol.Concat(*inputs, dim=axis)
            in_axis = axis

    if isinstance(inputs, symbol.Symbol) and axis != in_axis:
        inputs = symbol.swapaxes(inputs, dim0=axis, dim1=in_axis)

    return inputs, axis


class RNNParams(object):
    """Container for holding variables.
    Used by RNN cells for parameter sharing between cells.

    Parameters
    ----------
    prefix : str
        All variables' name created by this container will
        be prepended with prefix
    """
    def __init__(self, prefix=''):
        self._prefix = prefix
        self._params = {}

    def get(self, name, **kwargs):
        """Get a variable with name or create a new one if missing.

        Parameters
        ----------
        name : str
            name of the variable
        **kwargs :
            more arguments that's passed to symbol.Variable
        """
        name = self._prefix + name
        if name not in self._params:
            self._params[name] = symbol.Variable(name, **kwargs)
        return self._params[name]


class BaseRNNCell(object):
    """Abstract base class for RNN cells

    Parameters
    ----------
    prefix : str
        prefix for name of layers
        (and name of weight if params is None)
    params : RNNParams or None
        container for weight sharing between cells.
        created if None.
    """
    def __init__(self, prefix='', params=None):
        if params is None:
            params = RNNParams(prefix)
            self._own_params = True
        else:
            self._own_params = False
        self._prefix = prefix
        self._params = params
        self._modified = False

        self.reset()

    def reset(self):
        """Reset before re-using the cell for another graph"""
        self._init_counter = -1
        self._counter = -1

    def __call__(self, inputs, states):
        """Construct symbol for one step of RNN.

        Parameters
        ----------
        inputs : sym.Variable
            input symbol, 2D, batch * num_units
        states : sym.Variable
            state from previous step or begin_state().

        Returns
        -------
        output : Symbol
            output symbol
        states : Symbol
            state to next step of RNN.
        """
        raise NotImplementedError()

    @property
    def params(self):
        """Parameters of this cell"""
        self._own_params = False
        return self._params

    @property
    def state_info(self):
        """shape and layout information of states"""
        raise NotImplementedError()

    @property
    def state_shape(self):
        """shape(s) of states"""
        return [ele['shape'] for ele in self.state_info]

    @property
    def _gate_names(self):
        """name(s) of gates"""
        return ()

    def begin_state(self, func=symbol.zeros, **kwargs):
        """Initial state for this cell.

        Parameters
        ----------
        func : callable, default symbol.zeros
            Function for creating initial state. Can be symbol.zeros,
            symbol.uniform, symbol.Variable etc.
            Use symbol.Variable if you want to directly
            feed input as states.
        **kwargs :
            more keyword arguments passed to func. For example
            mean, std, dtype, etc.

        Returns
        -------
        states : nested list of Symbol
            starting states for first RNN step
        """
        assert not self._modified, \
            "After applying modifier cells (e.g. DropoutCell) the base " \
            "cell cannot be called directly. Call the modifier cell instead."
        states = []
        for info in self.state_info:
            self._init_counter += 1
            if info is None:
                state = func(name='%sbegin_state_%d'%(self._prefix, self._init_counter),
                             **kwargs)
            else:
                kwargs.update(info)
                state = func(name='%sbegin_state_%d'%(self._prefix, self._init_counter),
                             **kwargs)
            states.append(state)
        return states

    def unpack_weights(self, args):
        """Unpack fused weight matrices into separate
        weight matrices

        Parameters
        ----------
        args : dict of str -> NDArray
            dictionary containing packed weights.
            usually from Module.get_output()

        Returns
        -------
        args : dict of str -> NDArray
            dictionary with weights associated to
            this cell unpacked.
        """
        args = args.copy()
        if not self._gate_names:
            return args
        h = self._num_hidden
        for group_name in ['i2h', 'h2h']:
            weight = args.pop('%s%s_weight'%(self._prefix, group_name))
            bias = args.pop('%s%s_bias' % (self._prefix, group_name))
            for j, gate in enumerate(self._gate_names):
                wname = '%s%s%s_weight' % (self._prefix, group_name, gate)
                args[wname] = weight[j*h:(j+1)*h].copy()
                bname = '%s%s%s_bias' % (self._prefix, group_name, gate)
                args[bname] = bias[j*h:(j+1)*h].copy()
        return args

    def pack_weights(self, args):
        """Pack separate weight matrices into fused
        weight.

        Parameters
        ----------
        args : dict of str -> NDArray
            dictionary containing unpacked weights.

        Returns
        -------
        args : dict of str -> NDArray
            dictionary with weights associated to
            this cell packed.
        """
        args = args.copy()
        if not self._gate_names:
            return args
        for group_name in ['i2h', 'h2h']:
            weight = []
            bias = []
            for gate in self._gate_names:
                wname = '%s%s%s_weight'%(self._prefix, group_name, gate)
                weight.append(args.pop(wname))
                bname = '%s%s%s_bias'%(self._prefix, group_name, gate)
                bias.append(args.pop(bname))
            args['%s%s_weight'%(self._prefix, group_name)] = ndarray.concatenate(weight)
            args['%s%s_bias'%(self._prefix, group_name)] = ndarray.concatenate(bias)
        return args

    def unroll(self, length, inputs, begin_state=None, layout='NTC', merge_outputs=None):
        """Unroll an RNN cell across time steps.

        Parameters
        ----------
        length : int
            number of steps to unroll
        inputs : Symbol, list of Symbol, or None
            if inputs is a single Symbol (usually the output
            of Embedding symbol), it should have shape
            (batch_size, length, ...) if layout == 'NTC',
            or (length, batch_size, ...) if layout == 'TNC'.

            If inputs is a list of symbols (usually output of
            previous unroll), they should all have shape
            (batch_size, ...).
        begin_state : nested list of Symbol
            input states. Created by begin_state()
            or output state of another cell. Created
            from begin_state() if None.
        layout : str
            layout of input symbol. Only used if inputs
            is a single Symbol.
        merge_outputs : bool
            If False, return outputs as a list of Symbols.
            If True, concatenate output across time steps
            and return a single symbol with shape
            (batch_size, length, ...) if layout == 'NTC',
            or (length, batch_size, ...) if layout == 'TNC'.
            If None, output whatever is faster

        Returns
        -------
        outputs : list of Symbol
            output symbols.
        states : Symbol or nested list of Symbol
            has the same structure as begin_state()
        """
        self.reset()

        inputs, _ = _normalize_sequence(length, inputs, layout, False)
        if begin_state is None:
            begin_state = self.begin_state()

        states = begin_state
        outputs = []
        for i in range(length):
            output, states = self(inputs[i], states)
            outputs.append(output)

        outputs, _ = _normalize_sequence(length, outputs, layout, merge_outputs)

        return outputs, states

    #pylint: disable=no-self-use
    def _get_activation(self, inputs, activation, **kwargs):
        """Get activation function. Convert if is string"""
        if isinstance(activation, string_types):
            return symbol.Activation(inputs, act_type=activation, **kwargs)
        else:
            return activation(inputs, **kwargs)


class RNNCell(BaseRNNCell):
    """Simple recurrent neural network cell

    Parameters
    ----------
    num_hidden : int
        number of units in output symbol
    activation : str or Symbol, default 'tanh'
        type of activation function
    prefix : str, default 'rnn_'
        prefix for name of layers
        (and name of weight if params is None)
    params : RNNParams or None
        container for weight sharing between cells.
        created if None.
    """
    def __init__(self, num_hidden, activation='tanh', prefix='rnn_', params=None):
        super(RNNCell, self).__init__(prefix=prefix, params=params)
        self._num_hidden = num_hidden
        self._activation = activation
        self._iW = self.params.get('i2h_weight')
        self._iB = self.params.get('i2h_bias')
        self._hW = self.params.get('h2h_weight')
        self._hB = self.params.get('h2h_bias')

    @property
    def state_info(self):
        return [{'shape': (0, self._num_hidden), '__layout__': 'NC'}]

    @property
    def _gate_names(self):
        return ('',)

    def __call__(self, inputs, states):
        self._counter += 1
        name = '%st%d_'%(self._prefix, self._counter)
        i2h = symbol.FullyConnected(data=inputs, weight=self._iW, bias=self._iB,
                                    num_hidden=self._num_hidden,
                                    name='%si2h'%name)
        h2h = symbol.FullyConnected(data=states[0], weight=self._hW, bias=self._hB,
                                    num_hidden=self._num_hidden,
                                    name='%sh2h'%name)
        output = self._get_activation(i2h + h2h, self._activation,
                                      name='%sout'%name)

        return output, [output]


class LSTMCell(BaseRNNCell):
    """Long-Short Term Memory (LSTM) network cell.

    Parameters
    ----------
    num_hidden : int
        number of units in output symbol
    prefix : str, default 'rnn_'
        prefix for name of layers
        (and name of weight if params is None)
    params : RNNParams or None
        container for weight sharing between cells.
        created if None.
    forget_bias : bias added to forget gate, default 1.0.
        Jozefowicz et al. 2015 recommends setting this to 1.0
    """
    def __init__(self, num_hidden, prefix='lstm_', params=None, forget_bias=1.0):
        super(LSTMCell, self).__init__(prefix=prefix, params=params)

        self._num_hidden = num_hidden
        self._iW = self.params.get('i2h_weight')
        self._hW = self.params.get('h2h_weight')
        # we add the forget_bias to i2h_bias, this adds the bias to the forget gate activation
        self._iB = self.params.get('i2h_bias', init=init.LSTMBias(forget_bias=forget_bias))
        self._hB = self.params.get('h2h_bias')

    @property
    def state_info(self):
        return [{'shape': (0, self._num_hidden), '__layout__': 'NC'},
                {'shape': (0, self._num_hidden), '__layout__': 'NC'}]

    @property
    def _gate_names(self):
        return ['_i', '_f', '_c', '_o']

    def __call__(self, inputs, states):
        self._counter += 1
        name = '%st%d_'%(self._prefix, self._counter)
        i2h = symbol.FullyConnected(data=inputs, weight=self._iW, bias=self._iB,
                                    num_hidden=self._num_hidden*4,
                                    name='%si2h'%name)
        h2h = symbol.FullyConnected(data=states[0], weight=self._hW, bias=self._hB,
                                    num_hidden=self._num_hidden*4,
                                    name='%sh2h'%name)
        gates = i2h + h2h
        slice_gates = symbol.SliceChannel(gates, num_outputs=4,
                                          name="%sslice"%name)
        in_gate = symbol.Activation(slice_gates[0], act_type="sigmoid",
                                    name='%si'%name)
        forget_gate = symbol.Activation(slice_gates[1], act_type="sigmoid",
                                        name='%sf'%name)
        in_transform = symbol.Activation(slice_gates[2], act_type="tanh",
                                         name='%sc'%name)
        out_gate = symbol.Activation(slice_gates[3], act_type="sigmoid",
                                     name='%so'%name)
        next_c = symbol._internal._plus(forget_gate * states[1], in_gate * in_transform,
                                        name='%sstate'%name)
        next_h = symbol._internal._mul(out_gate, symbol.Activation(next_c, act_type="tanh"),
                                       name='%sout'%name)

        return next_h, [next_h, next_c]


class GRUCell(BaseRNNCell):
    """Gated Rectified Unit (GRU) network cell.
    Note: this is an implementation of the cuDNN version of GRUs
    (slight modification compared to Cho et al. 2014).

    Parameters
    ----------
    num_hidden : int
        number of units in output symbol
    prefix : str, default 'gru_'
        prefix for name of layers
        (and name of weight if params is None)
    params : RNNParams or None
        container for weight sharing between cells.
        created if None.
    """
    def __init__(self, num_hidden, prefix='gru_', params=None):
        super(GRUCell, self).__init__(prefix=prefix, params=params)
        self._num_hidden = num_hidden
        self._iW = self.params.get("i2h_weight")
        self._iB = self.params.get("i2h_bias")
        self._hW = self.params.get("h2h_weight")
        self._hB = self.params.get("h2h_bias")

    @property
    def state_info(self):
        return [{'shape': (0, self._num_hidden),
                 '__layout__': 'NC'}]

    @property
    def _gate_names(self):
        return ['_r', '_z', '_o']

    def __call__(self, inputs, states):
        # pylint: disable=too-many-locals
        self._counter += 1

        seq_idx = self._counter
        name = '%st%d_' % (self._prefix, seq_idx)
        prev_state_h = states[0]

        i2h = symbol.FullyConnected(data=inputs,
                                    weight=self._iW,
                                    bias=self._iB,
                                    num_hidden=self._num_hidden * 3,
                                    name="%s_i2h" % name)
        h2h = symbol.FullyConnected(data=prev_state_h,
                                    weight=self._hW,
                                    bias=self._hB,
                                    num_hidden=self._num_hidden * 3,
                                    name="%s_h2h" % name)

        i2h_r, i2h_z, i2h = symbol.SliceChannel(i2h, num_outputs=3, name="%s_i2h_slice" % name)
        h2h_r, h2h_z, h2h = symbol.SliceChannel(h2h, num_outputs=3, name="%s_h2h_slice" % name)

        reset_gate = symbol.Activation(i2h_r + h2h_r, act_type="sigmoid",
                                       name="%s_r_act" % name)
        update_gate = symbol.Activation(i2h_z + h2h_z, act_type="sigmoid",
                                        name="%s_z_act" % name)

        next_h_tmp = symbol.Activation(i2h + reset_gate * h2h, act_type="tanh",
                                       name="%s_h_act" % name)

        next_h = symbol._internal._plus((1. - update_gate) * next_h_tmp, update_gate * prev_state_h,
                                        name='%sout' % name)

        return next_h, [next_h]


class FusedRNNCell(BaseRNNCell):
    """Fusing RNN layers across time step into one kernel.
    Improves speed but is less flexible. Currently only
    supported if using cuDNN on GPU.

    Parameters
    ----------
    """
    def __init__(self, num_hidden, num_layers=1, mode='lstm', bidirectional=False,
                 dropout=0., get_next_state=False, forget_bias=1.0,
                 prefix=None, params=None):
        if prefix is None:
            prefix = '%s_'%mode
        super(FusedRNNCell, self).__init__(prefix=prefix, params=params)
        self._num_hidden = num_hidden
        self._num_layers = num_layers
        self._mode = mode
        self._bidirectional = bidirectional
        self._dropout = dropout
        self._get_next_state = get_next_state
        self._directions = ['l', 'r'] if bidirectional else ['l']

        initializer = init.FusedRNN(None, num_hidden, num_layers, mode,
                                    bidirectional, forget_bias)
        self._parameter = self.params.get('parameters', init=initializer)

    @property
    def state_info(self):
        b = self._bidirectional + 1
        n = (self._mode == 'lstm') + 1
        return [{'shape': (b*self._num_layers, 0, self._num_hidden), '__layout__': 'LNC'}
                for _ in range(n)]

    @property
    def _gate_names(self):
        return {'rnn_relu': [''],
                'rnn_tanh': [''],
                'lstm': ['_i', '_f', '_c', '_o'],
                'gru': ['_r', '_z', '_o']}[self._mode]

    @property
    def _num_gates(self):
        return len(self._gate_names)

    def _slice_weights(self, arr, li, lh):
        """slice fused rnn weights"""
        args = {}
        gate_names = self._gate_names
        directions = self._directions

        b = len(directions)
        p = 0
        for layer in range(self._num_layers):
            for direction in directions:
                for gate in gate_names:
                    name = '%s%s%d_i2h%s_weight'%(self._prefix, direction, layer, gate)
                    if layer > 0:
                        size = b*lh*lh
                        args[name] = arr[p:p+size].reshape((lh, b*lh))
                    else:
                        size = li*lh
                        args[name] = arr[p:p+size].reshape((lh, li))
                    p += size
                for gate in gate_names:
                    name = '%s%s%d_h2h%s_weight'%(self._prefix, direction, layer, gate)
                    size = lh**2
                    args[name] = arr[p:p+size].reshape((lh, lh))
                    p += size

        for layer in range(self._num_layers):
            for direction in directions:
                for gate in gate_names:
                    name = '%s%s%d_i2h%s_bias'%(self._prefix, direction, layer, gate)
                    args[name] = arr[p:p+lh]
                    p += lh
                for gate in gate_names:
                    name = '%s%s%d_h2h%s_bias'%(self._prefix, direction, layer, gate)
                    args[name] = arr[p:p+lh]
                    p += lh

        assert p == arr.size, "Invalid parameters size for FusedRNNCell"
        return args

    def unpack_weights(self, args):
        args = args.copy()
        arr = args.pop(self._parameter.name)
        b = len(self._directions)
        m = self._num_gates
        h = self._num_hidden
        num_input = arr.size//b//h//m - (self._num_layers - 1)*(h+b*h+2) - h - 2

        nargs = self._slice_weights(arr, num_input, self._num_hidden)
        args.update({name: nd.copy() for name, nd in nargs.items()})
        return args

    def pack_weights(self, args):
        args = args.copy()
        b = self._bidirectional + 1
        m = self._num_gates
        c = self._gate_names
        h = self._num_hidden
        w0 = args['%sl0_i2h%s_weight'%(self._prefix, c[0])]
        num_input = w0.shape[1]
        total = (num_input+h+2)*h*m*b + (self._num_layers-1)*m*h*(h+b*h+2)*b

        arr = ndarray.zeros((total,), ctx=w0.context, dtype=w0.dtype)
        for name, nd in self._slice_weights(arr, num_input, h).items():
            nd[:] = args.pop(name)
        args[self._parameter.name] = arr
        return args

    def __call__(self, inputs, states):
        raise NotImplementedError("FusedRNNCell cannot be stepped. Please use unroll")

    def unroll(self, length, inputs, begin_state=None, layout='NTC', merge_outputs=None):
        self.reset()

        inputs, axis = _normalize_sequence(length, inputs, layout, True)
        if axis == 1:
            warnings.warn("NTC layout detected. Consider using "
                          "TNC for FusedRNNCell for faster speed")
            inputs = symbol.swapaxes(inputs, dim1=0, dim2=1)
        else:
            assert axis == 0, "Unsupported layout %s"%layout
        if begin_state is None:
            begin_state = self.begin_state()

        states = begin_state
        if self._mode == 'lstm':
            states = {'state': states[0], 'state_cell': states[1]} # pylint: disable=redefined-variable-type
        else:
            states = {'state': states[0]}

        rnn = symbol.RNN(data=inputs, parameters=self._parameter,
                         state_size=self._num_hidden, num_layers=self._num_layers,
                         bidirectional=self._bidirectional, p=self._dropout,
                         state_outputs=self._get_next_state,
                         mode=self._mode, name=self._prefix+'rnn',
                         **states)

        if not self._get_next_state:
            outputs, states = rnn, []
        elif self._mode == 'lstm':
            outputs, states = rnn[0], [rnn[1], rnn[2]]
        else:
            outputs, states = rnn[0], [rnn[1]]

        if axis == 1:
            outputs = symbol.swapaxes(outputs, dim1=0, dim2=1)

        outputs, _ = _normalize_sequence(length, outputs, layout, merge_outputs)

        return outputs, states

    def unfuse(self):
        """Unfuse the fused RNN in to a stack of rnn cells.

        Returns
        -------
        cell : SequentialRNNCell
            unfused cell that can be used for stepping, and can run on CPU.
        """
        stack = SequentialRNNCell()
        get_cell = {'rnn_relu': lambda cell_prefix: RNNCell(self._num_hidden,
                                                            activation='relu',
                                                            prefix=cell_prefix),
                    'rnn_tanh': lambda cell_prefix: RNNCell(self._num_hidden,
                                                            activation='tanh',
                                                            prefix=cell_prefix),
                    'lstm': lambda cell_prefix: LSTMCell(self._num_hidden,
                                                         prefix=cell_prefix),
                    'gru': lambda cell_prefix: GRUCell(self._num_hidden,
                                                       prefix=cell_prefix)}[self._mode]
        for i in range(self._num_layers):
            if self._bidirectional:
                stack.add(BidirectionalCell(
                    get_cell('%sl%d_'%(self._prefix, i)),
                    get_cell('%sr%d_'%(self._prefix, i)),
                    output_prefix='%sbi_l%d_'%(self._prefix, i)))
            else:
                stack.add(get_cell('%sl%d_'%(self._prefix, i)))

            if self._dropout > 0 and i != self._num_layers - 1:
                stack.add(DropoutCell(self._dropout, prefix='%s_dropout%d_'%(self._prefix, i)))

        return stack


class SequentialRNNCell(BaseRNNCell):
    """Sequantially stacking multiple RNN cells

    Parameters
    ----------
    params : RNNParams or None
        container for weight sharing between cells.
        created if None.
    """
    def __init__(self, params=None):
        super(SequentialRNNCell, self).__init__(prefix='', params=params)
        self._override_cell_params = params is not None
        self._cells = []

    def add(self, cell):
        """Append a cell into the stack.

        Parameters
        ----------
        cell : rnn cell
        """
        self._cells.append(cell)
        if self._override_cell_params:
            assert cell._own_params, \
                "Either specify params for SequentialRNNCell " \
                "or child cells, not both."
            cell.params._params.update(self.params._params)
        self.params._params.update(cell.params._params)

    @property
    def state_info(self):
        return _cells_state_info(self._cells)

    def begin_state(self, **kwargs):
        assert not self._modified, \
            "After applying modifier cells (e.g. ZoneoutCell) the base " \
            "cell cannot be called directly. Call the modifier cell instead."
        return _cells_begin_state(self._cells, **kwargs)

    def unpack_weights(self, args):
        return _cells_unpack_weights(self._cells, args)

    def pack_weights(self, args):
        return _cells_pack_weights(self._cells, args)

    def __call__(self, inputs, states):
        self._counter += 1
        next_states = []
        p = 0
        for cell in self._cells:
            assert not isinstance(cell, BidirectionalCell)
            n = len(cell.state_info)
            state = states[p:p+n]
            p += n
            inputs, state = cell(inputs, state)
            next_states.append(state)
        return inputs, sum(next_states, [])

    def unroll(self, length, inputs, begin_state=None, layout='NTC', merge_outputs=None):
        self.reset()

        num_cells = len(self._cells)
        if begin_state is None:
            begin_state = self.begin_state()

        p = 0
        next_states = []
        for i, cell in enumerate(self._cells):
            n = len(cell.state_info)
            states = begin_state[p:p+n]
            p += n
            inputs, states = cell.unroll(length, inputs=inputs, begin_state=states, layout=layout,
                                         merge_outputs=None if i < num_cells-1 else merge_outputs)
            next_states.extend(states)

        return inputs, next_states


class DropoutCell(BaseRNNCell):
    """Apply dropout on input.

    Parameters
    ----------
    dropout : float
        percentage of elements to drop out, which
        is 1 - percentage to retain.
    """
    def __init__(self, dropout, prefix='dropout_', params=None):
        super(DropoutCell, self).__init__(prefix, params)
        assert isinstance(dropout, numeric_types), "dropout probability must be a number"
        self.dropout = dropout

    @property
    def state_info(self):
        return []

    def __call__(self, inputs, states):
        if self.dropout > 0:
            inputs = symbol.Dropout(data=inputs, p=self.dropout)
        return inputs, states

    def unroll(self, length, inputs, begin_state=None, layout='NTC', merge_outputs=None):
        self.reset()
        inputs, _ = _normalize_sequence(length, inputs, layout, merge_outputs)
        if isinstance(inputs, symbol.Symbol):
            return self(inputs, [])
        else:
            return super(DropoutCell, self).unroll(
                length, inputs, begin_state=begin_state, layout=layout,
                merge_outputs=merge_outputs)


class ModifierCell(BaseRNNCell):
    """Base class for modifier cells. A modifier
    cell takes a base cell, apply modifications
    on it (e.g. Zoneout), and returns a new cell.

    After applying modifiers the base cell should
    no longer be called directly. The modifer cell
    should be used instead.
    """
    def __init__(self, base_cell):
        super(ModifierCell, self).__init__()
        base_cell._modified = True
        self.base_cell = base_cell

    @property
    def params(self):
        self._own_params = False
        return self.base_cell.params

    @property
    def state_info(self):
        return self.base_cell.state_info

    def begin_state(self, init_sym=symbol.zeros, **kwargs):
        assert not self._modified, \
            "After applying modifier cells (e.g. DropoutCell) the base " \
            "cell cannot be called directly. Call the modifier cell instead."
        self.base_cell._modified = False
        begin = self.base_cell.begin_state(init_sym, **kwargs)
        self.base_cell._modified = True
        return begin

    def unpack_weights(self, args):
        return self.base_cell.unpack_weights(args)

    def pack_weights(self, args):
        return self.base_cell.pack_weights(args)

    def __call__(self, inputs, states):
        raise NotImplementedError


class ZoneoutCell(ModifierCell):
    """Apply Zoneout on base cell"""
    def __init__(self, base_cell, zoneout_outputs=0., zoneout_states=0.):
        assert not isinstance(base_cell, FusedRNNCell), \
            "FusedRNNCell doesn't support zoneout. " \
            "Please unfuse first."
        assert not isinstance(base_cell, BidirectionalCell), \
            "BidirectionalCell doesn't support zoneout since it doesn't support step. " \
            "Please add ZoneoutCell to the cells underneath instead."
        assert not isinstance(base_cell, SequentialRNNCell) or not base_cell._bidirectional, \
            "Bidirectional SequentialRNNCell doesn't support zoneout. " \
            "Please add ZoneoutCell to the cells underneath instead."
        super(ZoneoutCell, self).__init__(base_cell)
        self.zoneout_outputs = zoneout_outputs
        self.zoneout_states = zoneout_states
        self.prev_output = None

    def reset(self):
        super(ZoneoutCell, self).reset()
        self.prev_output = None

    def __call__(self, inputs, states):
        cell, p_outputs, p_states = self.base_cell, self.zoneout_outputs, self.zoneout_states
        next_output, next_states = cell(inputs, states)
        mask = (lambda p, like:
                symbol.Dropout(_symbol_internal._identity_with_attr_like_rhs(symbol.ones((0, 0)),
                                                                             like),
                               p=p))

        prev_output = self.prev_output if self.prev_output else symbol.zeros((0, 0))

        output = (symbol.where(mask(p_outputs, next_output), next_output, prev_output)
                  if p_outputs != 0. else next_output)
        states = ([symbol.where(mask(p_states, new_s), new_s, old_s) for new_s, old_s in
                   zip(next_states, states)] if p_states != 0. else next_states)

        self.prev_output = output

        return output, states



class BidirectionalCell(BaseRNNCell):
    """Bidirectional RNN cell

    Parameters
    ----------
    l_cell : BaseRNNCell
        cell for forward unrolling
    r_cell : BaseRNNCell
        cell for backward unrolling
    output_prefix : str, default 'bi_'
        prefix for name of output
    """
    def __init__(self, l_cell, r_cell, params=None, output_prefix='bi_'):
        super(BidirectionalCell, self).__init__('', params=params)
        self._override_cell_params = params is not None
        self._cells = [l_cell, r_cell]
        self._output_prefix = output_prefix

    def unpack_weights(self, args):
        return _cells_unpack_weights(self._cells, args)

    def pack_weights(self, args):
        return _cells_pack_weights(self._cells, args)

    def __call__(self, inputs, states):
        raise NotImplementedError("Bidirectional cannot be stepped. Please use unroll")

    @property
    def state_info(self):
        return _cells_state_info(self._cells)

    def begin_state(self, **kwargs):
        assert not self._modified, \
            "After applying modifier cells (e.g. DropoutCell) the base " \
            "cell cannot be called directly. Call the modifier cell instead."
        return _cells_begin_state(self._cells, **kwargs)

    def unroll(self, length, inputs, begin_state=None, layout='NTC', merge_outputs=None):
        self.reset()

        inputs, axis = _normalize_sequence(length, inputs, layout, False)
        if begin_state is None:
            begin_state = self.begin_state()

        states = begin_state
        l_cell, r_cell = self._cells
        l_outputs, l_states = l_cell.unroll(length, inputs=inputs,
                                            begin_state=states[:len(l_cell.state_info)],
                                            layout=layout, merge_outputs=merge_outputs)
        r_outputs, r_states = r_cell.unroll(length,
                                            inputs=list(reversed(inputs)),
                                            begin_state=states[len(l_cell.state_info):],
                                            layout=layout, merge_outputs=merge_outputs)

        if merge_outputs is None:
            merge_outputs = (isinstance(l_outputs, symbol.Symbol)
                             and isinstance(r_outputs, symbol.Symbol))
            if not merge_outputs:
                if isinstance(l_outputs, symbol.Symbol):
                    l_outputs = list(symbol.SliceChannel(l_outputs, axis=axis,
                                                         num_outputs=length, squeeze_axis=1))
                if isinstance(r_outputs, symbol.Symbol):
                    r_outputs = list(symbol.SliceChannel(r_outputs, axis=axis,
                                                         num_outputs=length, squeeze_axis=1))

        if merge_outputs:
            l_outputs = [l_outputs]
            r_outputs = [symbol.reverse(r_outputs, axis=axis)]
        else:
            r_outputs = list(reversed(r_outputs))

        outputs = [symbol.Concat(l_o, r_o, dim=1+merge_outputs,
                                 name=('%sout'%(self._output_prefix) if merge_outputs
                                       else '%st%d'%(self._output_prefix, i)))
                   for i, l_o, r_o in
                   zip(range(len(l_outputs)), l_outputs, r_outputs)]

        if merge_outputs:
            outputs = outputs[0]

        states = [l_states, r_states]
        return outputs, states
