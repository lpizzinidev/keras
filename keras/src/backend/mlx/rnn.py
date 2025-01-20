import contextlib

import mlx.core as mx

from keras.src import tree
from keras.src.backend.common import stateless_scope


def rnn(
    step_function,
    inputs,
    initial_states,
    go_backwards=False,
    mask=None,
    constants=None,
    unroll=False,
    input_length=None,
    time_major=False,
    zero_output_for_mask=False,
    return_all_outputs=True,
):
    def swap_batch_timestep(input_t):
        # Swap the batch and timestep dim for the incoming tensor.
        axes = list(range(len(input_t.shape)))
        axes[0], axes[1] = 1, 0
        return mx.transpose(input_t, axes)

    if not time_major:
        inputs = tree.map_structure(swap_batch_timestep, inputs)

    flattened_inputs = tree.flatten(inputs)
    time_steps = flattened_inputs[0].shape[0]

    if mask is not None:
        if mask.dtype != mx.bool_:
            mask = mask.astype(mx.bool_)
        if len(mask.shape) == 2:
            mask = mx.expand_dims(mask, axis=-1)
        if not time_major:
            mask = swap_batch_timestep(mask)

    if constants is None:
        constants = []

    def _expand_mask(mask_t, input_t, fixed_dim=1):
        if tree.is_nested(mask_t):
            raise ValueError(
                f"mask_t is expected to be tensor, but got {mask_t}"
            )
        if tree.is_nested(input_t):
            raise ValueError(
                f"input_t is expected to be tensor, but got {input_t}"
            )
        rank_diff = len(input_t.shape) - len(mask_t.shape)
        for _ in range(rank_diff):
            mask_t = mx.expand_dims(mask_t, axis=-1)
        multiples = [1] * fixed_dim + list(input_t.shape[fixed_dim:])
        return mx.tile(mask_t, multiples)

    if unroll:
        if not time_steps:
            raise ValueError("Unrolling requires a fixed number of timesteps.")
        states = tuple(initial_states)
        successive_states = []
        successive_outputs = []

        # Process the input tensors. The input tensor need to be split on the
        # time_step dim, and reverse if go_backwards is True. In the case of
        # nested input, the input is flattened and then transformed
        # individually.  The result of this will be a tuple of lists, each of
        # the item in tuple is list of the tensor with shape (batch, feature)
        def _process_single_input_t(input_t):
            input_t = unstack(input_t)  # unstack for time_step dim
            if go_backwards:
                input_t.reverse()
            return input_t

        if tree.is_nested(inputs):
            processed_input = tree.map_structure(
                _process_single_input_t, inputs
            )
        else:
            processed_input = (_process_single_input_t(inputs),)

        def _get_input_tensor(time):
            inp = [t_[time] for t_ in processed_input]
            return tree.pack_sequence_as(inputs, inp)

        if mask is not None:
            mask_list = unstack(mask)
            if go_backwards:
                mask_list.reverse()

            for i in range(time_steps):
                inp = _get_input_tensor(i)
                mask_t = mask_list[i]
                output, new_states = step_function(
                    inp, tuple(states) + tuple(constants)
                )
                tiled_mask_t = _expand_mask(mask_t, output)

                if not successive_outputs:
                    prev_output = mx.zeros_like(output)
                else:
                    prev_output = successive_outputs[-1]

                output = mx.where(tiled_mask_t, output, prev_output)

                flat_states = tree.flatten(states)
                flat_new_states = tree.flatten(new_states)
                tiled_mask_t = tuple(
                    _expand_mask(mask_t, s) for s in flat_states
                )
                flat_final_states = tuple(
                    mx.where(m, s, ps)
                    for m, s, ps in zip(
                        tiled_mask_t, flat_new_states, flat_states
                    )
                )
                states = tree.pack_sequence_as(states, flat_final_states)

                if return_all_outputs:
                    successive_outputs.append(output)
                    successive_states.append(states)
                else:
                    successive_outputs = [output]
                    successive_states = [states]
            last_output = successive_outputs[-1]
            new_states = successive_states[-1]
            outputs = mx.stack(successive_outputs)

        else:  # mask is None
            for i in range(time_steps):
                inp = _get_input_tensor(i)
                output, states = step_function(
                    inp, tuple(states) + tuple(constants)
                )
                if return_all_outputs:
                    successive_outputs.append(output)
                    successive_states.append(states)
                else:
                    successive_outputs = [output]
                    successive_states = [states]
            last_output = successive_outputs[-1]
            new_states = successive_states[-1]
            outputs = mx.stack(successive_outputs)

    else:  # Unroll == False
        if mask is not None:

            def _step(states, current_input):
                current_input, current_mask = current_input
                is_masked = mx.all(
                    mx.logical_not(current_mask), axis=-1, keepdims=True
                )

                output_t, new_states = step_function(current_input, states)

                if zero_output_for_mask:
                    masked_outs = mx.where(
                        is_masked, mx.zeros_like(output_t), output_t
                    )
                else:
                    # Assume the first state is the previous output.
                    output_tm1 = states[0]
                    masked_outs = mx.where(is_masked, output_tm1, output_t)

                new_states = [
                    mx.where(is_masked, s, ns)
                    for s, ns in zip(states, new_states)
                ]
                return (new_states, masked_outs)

            scan_xs = (inputs, mask)

        else:

            def _step(states, current_input):
                output_t, new_states = step_function(current_input, states)
                return new_states, output_t

            scan_xs = inputs
        if stateless_scope.in_stateless_scope():
            # Reuse the existing parent stateless scope.
            scope = contextlib.nullcontext()
        else:
            scope = stateless_scope.StatelessScope()
        with scope:
            new_states, outputs = mlx_scan(
                f=_step,
                init=initial_states,
                xs=scan_xs,
                reverse=go_backwards,
                mask=mask,
            )

        if go_backwards:
            outputs = reverse_sequence(outputs)

        last_output = outputs[-1]

    if not time_major:
        outputs = tree.map_structure(swap_batch_timestep, outputs)

    return last_output, outputs, new_states


def reverse_sequence(xs):
    indices = mx.arange(xs.shape[0] - 1, -1, -1)
    return mx.take(xs, indices, axis=0)


def unstack(x, axis=0):
    return [mx.take(x, i, axis=axis) for i in range(x.shape[axis])]


def mlx_scan(f, init, xs, reverse=False, mask=None):
    states = init
    outputs = []

    if mask is not None:
        x, mask = xs
        if reverse:
            x = reverse_sequence(x)
            mask = reverse_sequence(mask)

        for each_x, each_mask in zip(x, mask):
            states, output = f(states, (each_x, each_mask))
            outputs.append(output)
    else:
        if reverse:
            xs = reverse_sequence(xs)

        for x in xs:
            states, output = f(states, x)
            outputs.append(output)

    outputs = mx.array(outputs)

    if reverse:
        outputs = reverse_sequence(outputs)

    return states, outputs


def cudnn_ok(*args, **kwargs):
    return False


def lstm(*args, **kwargs):
    raise NotImplementedError("lstm not yet implemented in mlx")


def gru(*args, **kwargs):
    raise NotImplementedError("gru not yet implemented in mlx")
