"""
data_plots.py: Plotting functions for trial data.

This module encapsulates functions that produce Plotly figures of experimental data for display within the Lisberger
lab portal. By design, the function output is a Plotly figure or a Dash Graph object encapsulating a Plotly figure or,
in the event the plot cannot be generated, an HTML Div containing an error description. There are no Dash callbacks --
we want to be able to reuse these functions on different pages within the portal application.

@author: sruffner
@created: 20jan2022
"""
from typing import Dict, Any, Optional, Union, List

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from dash import html, dcc
import dash_bootstrap_components as dbc

from database import stats
from database.maestro import Protocol, SegParamType
from database.trial_data_ops import data_for_trial, retrieve_trial_reps_for_session, retrieve_trial_reps_for_neuron


_BEHAVIOR_TRACE_STYLE_MAP = {
    'HEPOS': dict(color='royalblue'),
    'HEVEL': dict(color='royalblue', dash='dot'),
    'HDVEL': dict(color='deepskyblue', dash='dot'),
    'VEPOS': dict(color='firebrick'),
    'VEVEL': dict(color='firebrick', dash='dot'),
    'FIX1_HPOS': dict(color='forestgreen'),
    'FIX1_VPOS': dict(color='gold'),
    'FIX2_HPOS': dict(color='orange'),
    'FIX2_VPOS': dict(color='orchid')
}
""" Maps Maestro behavioral or fixation target traces to a Plotly line style. """


def single_trial_response_figure(session: Dict[str, Any], trial_idx: int, unit_id: Optional[int] = None) -> \
        Union[html.Div, dcc.Graph]:
    """
    Generate a one- or two-figure plot displaying data from a single trial recorded during the specified experiment
    session. The top figure shows the position trajectory of the designated fixation target(s) and the recorded position
    and velocity trajectories of the eye. If a valid neural unit ID is specified, the bottom figure shows the neuron's
    recorded spike train and the derived firing rate as a function of time. If no unit is specified, the bottom figure
    is omitted.

    Given the notion of a "failsafe segment" in a Maestro trial, incomplete trials -- aborted because the animal did
    not satisfy fixation requirements at some point during the trial -- may be stored in the lab database. For such
    trials, the time axis of each figure spans the duration of the trial had it run to completion, and the fixation
    target trajectories are computed for the entire trial. The actual recorded response data (eye position/velocity and
    unit firing rate) will, of course, be truncated.

    Args:
        session: Dictionary with the primary key-value pairs that uniquely identify an experiment session.
        trial_idx: THe index of the trial.
        unit_id: If not None, this should identify a neural unit recorded during the session, and the function will
            render the unit's response during the trial in the bottom figure as described. If None, the bottom figure is
            omitted. Default = None.
    Returns:
        A Dash Graph component containing the Plotly figure of trial response data, as described above. If an error
            occurs while retrieving response data, the method instead returns an HTML Div with an error message.
    """
    trial_pk = {'experimenter': session['experimenter'], 'subj_id': session['subj_id'],
                'session_date': session['session_date'], 'session_sfx': session['session_sfx'],
                'trial_idx': trial_idx}
    trial_data = data_for_trial(trial_pk, unit_ids=None if (unit_id is None) else [unit_id])
    if trial_data is None:
        return html.Div(dbc.Alert(f"Failed to retrieve trial data for trial index {trial_idx}", is_open=True))

    fig = make_subplots(rows=1, cols=1, specs=[[{"secondary_y": True}]]) if (unit_id is None) \
        else make_subplots(rows=2, cols=1, specs=[[{"secondary_y": True}], [{"secondary_y": True}]],
                           shared_xaxes=True, vertical_spacing=0.04)

    hevel, vevel = trial_data.eye_velocity_saccades_removed()
    for response_id, trace in trial_data.behavior.items():
        adj_trace = hevel if response_id == 'HEVEL' else (vevel if response_id == 'VEVEL' else trace)
        fig.add_trace(
            go.Scatter(x=[i for i in range(len(adj_trace))], y=adj_trace, name=response_id, mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP[response_id], connectgaps=False,
                       yaxis='y2' if response_id.find('VEL') > -1 else None),
            row=1, col=1, secondary_y=(response_id.find('VEL') > -1)
        )

    # plot fixation target #1 H,V trajectories, if defined. Also use a thin translucent horizontal bar to highlight the
    # ON epochs for the fixation target #1, and label with the text annotation "Fix1 ON"
    duration_ms = trial_data.protocol.duration_of_rep(trial_data.trial_rvs)
    fix1_pos, fix2_pos = trial_data.protocol.compute_fixation_target_trajectories(
        trial_data.trial_rvs,
        trial_data.behavior['HEPOS'] if 'HEPOS' in trial_data.behavior else None,
        trial_data.behavior['VEPOS'] if 'VEPOS' in trial_data.behavior else None,
        trial_data.vstab_win_len_ms
    )
    fix1_on, fix2_on = trial_data.protocol.compute_fixation_target_on_epochs(trial_data.trial_rvs)
    if fix1_pos is not None:
        fig.add_trace(
            go.Scatter(x=[i for i in range(duration_ms)], y=fix1_pos[:, 0], name='FIX1_HPOS', mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP['FIX1_HPOS'], connectgaps=False),
            row=1, col=1, secondary_y=False
        )
        fig.add_trace(
            go.Scatter(x=[i for i in range(duration_ms)], y=fix1_pos[:, 1], name='FIX1_VPOS', mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP['FIX1_VPOS'], connectgaps=False),
            row=1, col=1, secondary_y=False
        )
        for i in range(0, len(fix1_on), 2):
            fig.add_shape(type='rect', x0=fix1_on[i], x1=fix1_on[i+1], xref='x', y0=0.05, y1=0.1, yref='y domain',
                          fillcolor=_BEHAVIOR_TRACE_STYLE_MAP['FIX1_HPOS']['color'], line=dict(width=0), opacity=0.2,
                          row=1, col=1)
            if i == 0:
                fig.add_annotation(x=fix1_on[0], y=0.075, yref='y domain', text='<b>Fix1 ON</b>', showarrow=False,
                                   ax=0, ay=0, xanchor="left", yanchor="middle", row=1, col=1)

    # and analogously for fixation target #2...
    if fix2_pos is not None:
        fig.add_trace(
            go.Scatter(x=[i for i in range(duration_ms)], y=fix2_pos[:, 0], name='FIX2_HPOS', mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP['FIX2_HPOS'], connectgaps=False),
            row=1, col=1, secondary_y=False
        )
        fig.add_trace(
            go.Scatter(x=[i for i in range(duration_ms)], y=fix2_pos[:, 1], name='FIX2_VPOS', mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP['FIX2_VPOS'], connectgaps=False),
            row=1, col=1, secondary_y=False
        )
        for i in range(0, len(fix2_on), 2):
            fig.add_shape(type='rect', x0=fix2_on[i], x1=fix2_on[i+1], xref='x', y0=0.12, y1=0.17, yref='y domain',
                          fillcolor=_BEHAVIOR_TRACE_STYLE_MAP['FIX2_HPOS']['color'], line=dict(width=0), opacity=0.2,
                          row=1, col=1)
            if i == 0:
                fig.add_annotation(x=fix2_on[0], y=0.145, yref='y domain', text='<b>Fix2 ON</b>', showarrow=False,
                                   ax=0, ay=0, xanchor="left", yanchor="middle", row=1, col=1)

    # if neural unit was specified, plot both its "spike train" and instantaneous firing rate in the bottom plot. Else,
    # the bottom plot is omitted.
    if unit_id is not None:
        firing_rate_trace = trial_data.instantaneous_firing_rate(unit_id, smooth=True)
        fig.add_trace(
            go.Scatter(x=[i for i in range(len(firing_rate_trace))], y=firing_rate_trace, name=f"Unit #{unit_id}",
                       mode='lines', connectgaps=False, line=dict(color='black', width=2), yaxis='y3'),
            row=2, col=1, secondary_y=False
        )

        x_spikes = list()
        y_spikes = list()
        for t in trial_data.neuronal[unit_id]:
            x_spikes.extend([t * 1000, t * 1000, None])
            y_spikes.extend([9, 10, None])
        fig.add_trace(
            go.Scatter(x=x_spikes, y=y_spikes, name=f"Unit #{unit_id} spikes", mode='lines', connectgaps=False,
                       line=dict(color='blue', width=2), yaxis='y4'),
            row=2, col=1, secondary_y=True
        )

    # segment spans defined by alternating blue and gray bars along top of top plot, with segment label.
    t = 0
    for i, seg in enumerate(trial_data.protocol.trial.segments):
        fig.add_shape(
            type='rect', x0=t, x1=t+seg.dur, xref='x', y0=0, y1=28, yanchor=1.01, yref='y domain', ysizemode='pixel',
            fillcolor='lightsteelblue' if (i % 2) == 0 else 'whitesmoke', line=dict(width=0),
            row=1, col=1, secondary_y=False
        )
        fig.add_annotation(
            x=t, y=1.01, yref='y domain', yshift=14, text=f"<b>Seg{i}</b>", showarrow=False, ax=0, ay=0,
            xanchor='left', yanchor='middle',
            row=1, col=1, secondary_y=False
        )
        t += seg.dur

    # Note top margin is larger to accommodate the segment labels rendered along the top plot.
    fig.update_layout(
        margin=dict(l=20, r=20, t=60, b=20),
        height=800,
        xaxis=dict(domain=[0, 0.95], title='time (milliseconds)' if (unit_id is None) else None),
        xaxis2=None if (unit_id is None) else dict(domain=[0, 0.95], title='time (milliseconds)'),
        yaxis=dict(title='position (degrees)'),
        yaxis2=dict(title='velocity (degrees/second)', anchor="x", overlaying="y", side="right"),
        yaxis3=None if (unit_id is None) else dict(title='firing rate (Hz)'),
        yaxis4=None if (unit_id is None) else dict(range=[0, 10], anchor="x", overlaying="y3", visible=False)
    )

    # if trial was not completed successfully, we add an annotation to the upper plot to indicate that trial aborted
    # prematurely. Also, if a neural response is displayed in the bottom plot, that plot will only span the recorded
    # duration, rather than the expected duration of the trial. SO we force the lower plot to the same x-axis range as
    # the upper plot
    if not trial_data.success:
        fig.add_annotation(x=0, y=1, yref='y domain', text='<b>** TRIAL NOT COMPLETED **</b>', showarrow=False,
                           ax=0, ay=0, xanchor="left", yanchor="top", row=1, col=1)
        if unit_id is not None:
            fig.update_xaxes(row=2, col=1, range=[0, duration_ms])

    return dcc.Graph(figure=fig)


def average_response_figure(session: Dict[str, Any], proto_hash: str, unit_id: Optional[int] = None) -> \
        Union[html.Div, dcc.Graph]:
    """
    Generate a two-figure plot displaying the mean behavioral and neuronal response across all recorded reps of the
    specified trial protocol THAT WERE COMPLETED SUCCESSFULLY. The top figure shows the position trajectories of the
    targets designated as "Fixation Target #1, #2", along with the average eye velocity trajectory. The bottom figure
    shows the specified neuron's mean firing rate during the trial, with a +/-1 SEM (standard error of the mean) band.
    If no neuron is specified, the bottom figure is omitted.

    The timeline in both figures is that portion of the trial protocol that is shared across all reps -- if a protocol
    includes a random-duration segment, then each rep will have a different duration overall. The method averages the
    successful reps across all fixed-duration segments, and across the last T milliseconds of the random-duration
    segment, where T is the minimum observed duration of that segment across all reps. Of course, this means there is a
    discontinuity in the average response at the end of the segment preceding the random-duration segment.

    Note that the average response figure is only generated if: (1) there are at least 3 SUCCESSFULLY COMPLETED reps of
    the given protocol during the experiment session; (2) the protocol definition is conducive to averaging -- that is,
    it has at MOST one random variable, which varies the duration of a single segment (not necessarily the first one).

    Args:
        session: Dictionary with the primary key-value pairs that uniquely identify an experiment session.
        proto_hash: The MD5 hash digest that uniquely identifies the trial protocol in the lab database.
        unit_id: If not None, this should identify a neural unit recorded during the session, and the function will
            render the unit's mean response in the bottom figure as described. If None, the bottom figure is omitted.
            Default = None.
    Returns:
        A Dash Graph component containing the mean behavioral and neuronal responses over all successfully completed
            reps of the specified trial protocol, as described above. If an error occurs while retrieving or processing
            response data, the method instead returns an HTML Div with an error message.
    """
    # retrieve trial data for all relevant trials recorded during session
    if unit_id is None:
        trial_data = retrieve_trial_reps_for_session(session, proto_hash)
    else:
        unit_key = session.copy()
        unit_key['unit_id'] = unit_id
        trial_data = retrieve_trial_reps_for_neuron(unit_key, proto_hash)

    if trial_data is None:
        return html.Div(dbc.Alert(f"Failed to retrieve trial data (internal error).", is_open=True))
    elif len(trial_data) < 3:
        return html.Div(dbc.Alert(f"Fewer than 3 successful trial reps ({len(trial_data)}) found for selected protocol",
                                  is_open=True))
    elif not trial_data[0].protocol.can_aggregate_responses():
        return html.Div(dbc.Alert("Selected protocol is not conducive to averaging across trial reps", is_open=True))

    protocol = trial_data[0].protocol
    min_dur = 0
    vary_dur_seg = -1
    hevel_list = list()
    vevel_list = list()
    for td in trial_data:
        h, v = td.eye_velocity_saccades_removed()
        hevel_list.append(h)
        vevel_list.append(v)

    firing_rate_list: Optional[List[np.ndarray]] = \
        None if (unit_id is None) else [td.instantaneous_firing_rate(unit_id, smooth=True) for td in trial_data]
    firing_rate = None
    sem_fr = None
    if len(protocol.rvs) == 0:
        hevel = np.nanmean(hevel_list, axis=0)
        vevel = np.nanmean(vevel_list, axis=0)
        if firing_rate_list:
            firing_rate = np.nanmean(firing_rate_list, axis=0)
            sem_fr = np.nanstd(firing_rate_list, axis=0) / np.sqrt(len(firing_rate_list))
        fix1_pos, fix2_pos = protocol.compute_fixation_target_trajectories([])
        fix1_on, fix2_on = protocol.compute_fixation_target_on_epochs([])
        t_vec = [i for i in range(len(hevel))]
    else:
        # the RV is the duration of a segment -- not necessarily the first one. For the random-duration segment, we
        # only average over the last T ms of that segment, where T is the minimum observed duration across trial reps.
        # This implies a "discontinuity" in the mean response traces.
        vary_dur_seg = protocol.rvs[0].seg_idx
        min_dur = int(min([td.trial_rvs[0] for td in trial_data]) + 0.5)
        prelude = sum([protocol.trial.segments[i].dur for i in range(vary_dur_seg)])

        hevel = np.concatenate(
            (np.nanmean([hevel_list[i][0:prelude] for i in range(len(trial_data))], axis=0),
             np.nanmean([hevel_list[i][prelude+td.trial_rvs[0]-min_dur:] for i, td in enumerate(trial_data)], axis=0)),
            axis=0
        )
        vevel = np.concatenate(
            (np.nanmean([vevel_list[i][0:prelude] for i in range(len(trial_data))], axis=0),
             np.nanmean([vevel_list[i][prelude+td.trial_rvs[0]-min_dur:] for i, td in enumerate(trial_data)], axis=0)),
            axis=0
        )

        if firing_rate_list:
            firing_rate_pre = [firing_rate_list[i][0:prelude] for i in range(len(trial_data))]
            firing_rate_post = \
                [firing_rate_list[i][prelude+td.trial_rvs[0]-min_dur:] for i, td in enumerate(trial_data)]
            firing_rate = np.concatenate(
                (np.nanmean(firing_rate_pre, axis=0), np.nanmean(firing_rate_post, axis=0)),
                axis=0
            )
            sem_fr = np.concatenate(
                (np.nanstd(firing_rate_pre, axis=0), np.nanstd(firing_rate_post, axis=0)),
                axis=0
            ) / np.sqrt(len(firing_rate_list))

        # we compute fixation target position trajectories and ON epoch times for the trial rep that had the minimum
        # observed duration for the random-duration segment.
        fix1_pos, fix2_pos = protocol.compute_fixation_target_trajectories([min_dur])
        fix1_on, fix2_on = protocol.compute_fixation_target_on_epochs([min_dur])

        t_vec = [i for i in range(len(hevel))]

    # one or two subplots: Average eye velocity and fixation target position trajectories in top plot, and mean +/-1 STD
    # firing rate of neuron in the bottom plot -- if a neural unit was specified
    fig = make_subplots(rows=1, cols=1, specs=[[{"secondary_y": True}]]) if (unit_id is None) else \
        make_subplots(rows=2, cols=1, specs=[[{"secondary_y": True}], [{"secondary_y": False}]],
                      shared_xaxes=True, vertical_spacing=0.04)

    fig.add_trace(
        go.Scatter(x=t_vec, y=hevel, name='HEVEL', mode='lines', line=_BEHAVIOR_TRACE_STYLE_MAP['HEVEL'], yaxis='y2'),
        row=1, col=1, secondary_y=True
    )
    fig.add_trace(
        go.Scatter(x=t_vec, y=vevel, name='VEVEL', mode='lines', line=_BEHAVIOR_TRACE_STYLE_MAP['VEVEL'], yaxis='y2'),
        row=1, col=1, secondary_y=True
    )

    # plot fixation target #1 H,V trajectories, if defined. Also use a thin translucent horizontal bar to highlight the
    # ON epochs for the fixation target #1, and label with the text annotation "Fix1 ON"
    if fix1_pos is not None:
        fig.add_trace(
            go.Scatter(x=t_vec, y=fix1_pos[:, 0], name='FIX1_HPOS', mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP['FIX1_HPOS'], connectgaps=False),
            row=1, col=1, secondary_y=False
        )
        fig.add_trace(
            go.Scatter(x=t_vec, y=fix1_pos[:, 1], name='FIX1_VPOS', mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP['FIX1_VPOS'], connectgaps=False),
            row=1, col=1, secondary_y=False
        )
        for i in range(0, len(fix1_on), 2):
            fig.add_shape(type='rect', x0=fix1_on[i], x1=fix1_on[i+1], xref='x', y0=0.05, y1=0.1, yref='y domain',
                          fillcolor=_BEHAVIOR_TRACE_STYLE_MAP['FIX1_HPOS']['color'], line=dict(width=0), opacity=0.2,
                          row=1, col=1)
            if i == 0:
                fig.add_annotation(x=fix1_on[0], y=0.075, yref='y domain', text='<b>Fix1 ON</b>', showarrow=False,
                                   ax=0, ay=0, xanchor="left", yanchor="middle", row=1, col=1)

    # ... and analogously for fixation target #2...
    if fix2_pos is not None:
        fig.add_trace(
            go.Scatter(x=t_vec, y=fix2_pos[:, 0], name='FIX2_HPOS', mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP['FIX2_HPOS'], connectgaps=False),
            row=1, col=1, secondary_y=False
        )
        fig.add_trace(
            go.Scatter(x=t_vec, y=fix2_pos[:, 1], name='FIX2_VPOS', mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP['FIX2_VPOS'], connectgaps=False),
            row=1, col=1, secondary_y=False
        )

        for i in range(0, len(fix2_on), 2):
            fig.add_shape(type='rect', x0=fix2_on[i], x1=fix2_on[i+1], xref='x', y0=0.12, y1=0.17, yref='y domain',
                          fillcolor=_BEHAVIOR_TRACE_STYLE_MAP['FIX2_HPOS']['color'], line=dict(width=0), opacity=0.2,
                          row=1, col=1)
            if i == 0:
                fig.add_annotation(x=fix2_on[0], y=0.145, yref='y domain', text='<b>Fix2 ON</b>', showarrow=False,
                                   ax=0, ay=0, xanchor="left", yanchor="middle", row=1, col=1)

    # neural response goes in bottom subplot unless no neural unit was specified
    if firing_rate is not None:
        fig.add_trace(
            go.Scatter(x=t_vec, y=firing_rate, mode='lines', connectgaps=False, line=dict(color='black', width=2),
                       name=f"Unit #{unit_id}", yaxis='y3'),
            row=2, col=1, secondary_y=False
        )
        fig.add_trace(
            go.Scatter(x=t_vec, y=firing_rate+sem_fr, mode='lines', connectgaps=True, line=dict(width=0),
                       name="+1STD", yaxis='y3', showlegend=False),
            row=2, col=1, secondary_y=False
        )
        fig.add_trace(
            go.Scatter(x=t_vec, y=firing_rate-sem_fr, mode='lines', connectgaps=True, line=dict(width=0),
                       name="-1STD", yaxis='y3', fillcolor='rgba(68, 68, 68, 0.3)', fill='tonexty', showlegend=False),
            row=2, col=1, secondary_y=False
        )

    # segment spans defined by alternating blue and gray bars along top of top plot, with segment label.
    t = 0
    for i, seg in enumerate(protocol.trial.segments):
        seg_dur = min_dur if (i == vary_dur_seg) else seg.dur
        fig.add_shape(
            type='rect', x0=t, x1=t+seg_dur, xref='x', y0=0, y1=28, yref='y domain', yanchor=1.01, ysizemode='pixel',
            fillcolor='lightsteelblue' if (i % 2) == 0 else 'whitesmoke', line=dict(width=0),
            row=1, col=1, secondary_y=False
        )
        fig.add_annotation(
            x=t, y=1.01, yref='y domain', yshift=14, text=f"<b>Seg{i}</b>", showarrow=False, ax=0, ay=0,
            xanchor='left', yanchor='middle',
            row=1, col=1, secondary_y=False
        )
        t += seg_dur

    # indicate the number of trial reps aggregated to produce this figure
    fig.add_annotation(
        x=0, y=1, yref='y domain', text=f"<b>N = {len(trial_data)} reps</b>", showarrow=False,
        xanchor='left', yanchor='top', row=1, col=1, secondary_y=False, font=dict(size=16)
    )

    # Note top margin is larger to accommodate the segment labels rendered along the top plot.
    fig.update_layout(
        margin=dict(l=20, r=20, t=60, b=20),
        height=800,
        xaxis=dict(domain=[0, 0.95], title='time (milliseconds)' if (unit_id is None) else None),
        xaxis2=None if (unit_id is None) else dict(domain=[0, 0.95], title='time (milliseconds)'),
        yaxis=dict(title='position (degrees)'),
        yaxis2=dict(title='velocity (degrees/second)', anchor="x", overlaying="y", side="right"),
        yaxis3=None if (unit_id is None) else dict(title='firing rate (Hz) [mean +/- 1STD]')
    )

    # highlight the portion of the figure that represents the random-duration segment with a translucent rectangle
    # spanning the two plots vertically, plus, a vertical dashed line at the start of that segment
    if min_dur > 0:
        t_start = sum([protocol.trial.segments[i].dur for i in range(vary_dur_seg)])
        fig.add_vrect(x0=t_start, x1=t_start+min_dur, fillcolor="red", opacity=0.2)
        fig.add_vline(x=t_start, line=dict(dash='dash', color='darkred', width=3), opacity=0.4)

    return dcc.Graph(figure=fig)


def discharge_statistics_figure(session: Dict[str, Any], proto_hash: str, unit_id: int,
                                seg_range: Optional[List[int]] = None) -> go.Figure:
    """
    Compute and plot the autocorrelogram (ACG) and inter-spike interval (ISI) histogram for the specified neural unit
    across all successfully completed reps of a particular trial protocol during the specified experiment session. The
    two histograms are rendered in side-by-side subplots.

    Args:
        session: Dictionary with the primary key-value pairs that uniquely identify an experiment session.
        proto_hash: The MD5 hash digest that uniquely identifies the trial protocol in the lab database.
        unit_id: Integer ID for a neural unit recorded during the session.
        seg_range: The contiguous interval [S, E] of trial segments over which the ACG and ISI should be computed. S
            lies in [0..N) and E in (0..N], where N is the number of segments in the trial protocol. Segment index N
            corresponds to trial's end. Default is None, in which case the computation covers the entire trial.
    Returns:
        The prepared Plotly figure displaying the computed ACG and ISI histogram. If an error occurs while retrieving
            trial data or computing the histograms, the two subplots in the figure will be empty except for a text
            annotation indicating an error.
    """
    isi: Optional[np.ndarray] = None
    acg: Optional[np.ndarray] = None
    num_spikes_in_acg = 0
    error_msg = None
    try:
        unit_key = session.copy()
        unit_key['unit_id'] = unit_id
        trial_data = retrieve_trial_reps_for_neuron(unit_key, proto_hash)
        if trial_data is None:
            raise Exception("Failed to retrieve data")
        protocol = trial_data[0].protocol
        num_segs = len(protocol.trial.segments)
        rand_dur_seg = -1 if len(protocol.rvs) == 0 else protocol.rvs[0].seg_idx
        seg_durations = [seg.dur for seg in protocol.trial.segments]
        for td in trial_data:
            spike_times = td.neuronal[unit_id]
            if (seg_range is not None) and ((seg_range[1] - seg_range[0]) < num_segs):
                if rand_dur_seg > -1:
                    seg_durations[rand_dur_seg] = td.trial_rvs[0]
                start = sum([seg_durations[i] for i in range(seg_range[0])])
                stop = sum([seg_durations[i] for i in range(seg_range[1])])
                spike_times = spike_times[np.logical_and(spike_times >= 1e-3*start, spike_times < 1e-3*stop)]
            if len(spike_times) < 2:
                continue
            isi_for_trial = stats.generate_isi_histogram(spike_times)
            isi = isi_for_trial if isi is None else (isi + isi_for_trial)
            acg_for_trial, n = stats.generate_cross_correlogram(spike_times, spike_times)
            acg = acg_for_trial if acg is None else (acg + acg_for_trial)
            num_spikes_in_acg += n
        # convert counts per bin to relative probability to Hz (1ms bins). Also, NaN the lag = 0 bin (trigger spike
        # is perfectly corralated with itself!)
        if num_spikes_in_acg > 0:
            acg = (acg / num_spikes_in_acg) * 1000
            acg[100] = np.nan

    except Exception:
        error_msg = "Unable to retrieve/compute data"
        isi = None
        acg = None
        num_spikes_in_acg = 0

    ds_plot = make_subplots(
        rows=1, cols=2, subplot_titles=(f"Autocorrelogram (N={num_spikes_in_acg})", 'Inter-spike Interval Histogram')
    )
    if not (acg is None):
        ds_plot.add_trace(go.Scatter(x=[i for i in range(-100, 101)], y=acg, mode='lines', connectgaps=False),
                          row=1, col=1)
    if not (isi is None):
        ds_plot.add_trace(go.Scatter(x=[i for i in range(0, 101)], y=isi, mode='lines', xaxis='x2', yaxis='y2'),
                          row=1, col=2)
    if not (error_msg is None):
        ds_plot.add_annotation(
            x=0.5, y=0.5, xref='paper', yref='paper', xanchor='center', yanchor='middle', showarrow=False,
            text=f"<b>{error_msg}</b>", row=1, col=1, secondary_y=False, font=dict(size=10)
        )
        ds_plot.add_annotation(
            x=0.5, y=0.5, xref='paper', yref='paper', xanchor='center', yanchor='middle', showarrow=False,
            text=f"<b>{error_msg}</b>", row=1, col=2, secondary_y=False, font=dict(size=10)
        )

    ds_plot.update_layout(
        margin=dict(l=20, r=20, t=30, b=20),
        xaxis=dict(title='lag (milliseconds)'),
        yaxis=dict(title='firing rate (Hz)'),
        xaxis2=dict(title='ISI (ms)'),
        yaxis2=dict(title='counts per bin'),
        showlegend=False
    )

    return ds_plot


def trial_target_trajectory_figure(proto: Protocol, rand_seg_dur: int = 0) -> go.Figure:
    """
    Generate a plot of the trajectories of the fixation target(s) during presentation of the specified trial protocol.
    The trial segment durations are laid out in a narrow labelled band of alternating colors across the top of the plot.
    The figure essentially serves as a visual rendering of what happens during any presentation of the trial protocol.
    No behavioral or neural response data are included.

    Args:
        proto: The trial protocol definition.
        rand_seg_dur: If the trial protocol includes a single random variable defining the duration of one of the trial
            segments, this parameter specifies the duration (in ms) to use for that segment. If there is no such RV,
            this argument is ignored. Default = 0.
    Returns:
        The prepared Plotly figure displaying the computed trial target trajectories.
    """
    rv_seg_idx = -1
    if (len(proto.rvs) == 1) and (proto.rvs[0].type == SegParamType.DURATION):
        rv_seg_idx = proto.rvs[0].seg_idx
        seg_dur = rand_seg_dur if rand_seg_dur > 0 else proto.trial.segments[rv_seg_idx].dur
        fix1_pos, fix2_pos = proto.compute_fixation_target_trajectories([seg_dur])
        fix1_on, fix2_on = proto.compute_fixation_target_on_epochs([seg_dur])
    else:
        fix1_pos, fix2_pos = proto.compute_fixation_target_trajectories([])
        fix1_on, fix2_on = proto.compute_fixation_target_on_epochs([])

    proto_plot = go.Figure()
    if fix1_pos is not None:
        proto_plot.add_trace(
            go.Scatter(y=fix1_pos[:, 0], name='FIX1_HPOS', mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP['FIX1_HPOS'], connectgaps=False))
        proto_plot.add_trace(
            go.Scatter(y=fix1_pos[:, 1], name='FIX1_VPOS', mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP['FIX1_VPOS'], connectgaps=False))
        for i in range(0, len(fix1_on), 2):
            proto_plot.add_shape(
                type='rect', x0=fix1_on[i], x1=fix1_on[i + 1], xref='x', y0=0.05, y1=0.1, yref='y domain',
                fillcolor=_BEHAVIOR_TRACE_STYLE_MAP['FIX1_HPOS']['color'], line=dict(width=0), opacity=0.2
            )
            if i == 0:
                proto_plot.add_annotation(
                    x=fix1_on[0], y=0.075, yref='y domain', text='<b>Fix1 ON</b>', showarrow=False,
                    ax=0, ay=0, xanchor="left", yanchor="middle"
                )
    if fix2_pos is not None:
        proto_plot.add_trace(
            go.Scatter(y=fix2_pos[:, 0], name='FIX2_HPOS', mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP['FIX2_HPOS'], connectgaps=False))
        proto_plot.add_trace(
            go.Scatter(y=fix2_pos[:, 1], name='FIX2_VPOS', mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP['FIX2_VPOS'], connectgaps=False))
        for i in range(0, len(fix2_on), 2):
            proto_plot.add_shape(
                type='rect', x0=fix2_on[i], x1=fix2_on[i + 1], xref='x', y0=0.12, y1=0.17, yref='y domain',
                fillcolor=_BEHAVIOR_TRACE_STYLE_MAP['FIX2_HPOS']['color'], line=dict(width=0), opacity=0.2
            )
            if i == 0:
                proto_plot.add_annotation(
                    x=fix2_on[0], y=0.145, yref='y domain', text='<b>Fix2 ON</b>', showarrow=False,
                    ax=0, ay=0, xanchor="left", yanchor="middle"
                )

    # segment spans defined by alternating blue and gray bars along top of protocol plot, with segment label.
    t = 0
    for i, seg in enumerate(proto.trial.segments):
        seg_dur = rand_seg_dur if i == rv_seg_idx else seg.dur
        proto_plot.add_shape(
            type='rect', x0=t, x1=t+seg_dur, xref='x', y0=0, y1=28, yref='y domain', yanchor=1.01, ysizemode='pixel',
            fillcolor='lightsteelblue' if (i % 2) == 0 else 'whitesmoke', line=dict(width=0)
        )
        proto_plot.add_annotation(
            x=t, y=1.01, yref='y domain', yshift=14, text=f"<b>Seg{i}</b>", showarrow=False, ax=0, ay=0,
            xanchor='left', yanchor='middle'
        )
        t += seg_dur

    # highlight the portion of the figure that represents the random-duration segment with a translucent rectangle
    # spanning the plot vertically, plus, a vertical dashed line at the start of that segment
    if (rv_seg_idx > -1) and (rand_seg_dur > 0):
        t_start = sum([proto.trial.segments[i].dur for i in range(rv_seg_idx)])
        proto_plot.add_vrect(x0=t_start, x1=t_start + rand_seg_dur, fillcolor="red", opacity=0.2)
        proto_plot.add_vline(x=t_start, line=dict(dash='dash', color='darkred', width=3), opacity=0.4)

    proto_plot.update_layout(
        margin=dict(l=20, r=20, t=60, b=20),
        height=300,
        xaxis=dict(title='time (milliseconds)'),
        yaxis=dict(title='position (degrees)'),
        showlegend=False
    )
    return proto_plot
