# Lisberger Lab Data Portal - Changelog

## v0.5.1 (11/2/2023)
- Tested commit process on a couple sample archives from N. Hall and addressed programming errors in 
`maestro.Perturbation.from_trial_codes()` and `commit_ops._SessionCommitMgr.insert_trials_for_session()`.
- Given the minor changes to PL2.py and maestro.py, which are part of the `sglportalapi` package, that package
has been rebuilt.

## v0.5.0 (10/31/2023)

- Began documenting changes. See Gitlab commit history for more information on how the project has evolved to this
point. Also added a `README`.
- Incorporated a change in from the `XSort` project -- `PL2.load_analog_channel_block_faster()` is ~100x faster than the
`load_analog_channel_block()` method. Session archive preprocessing updated to use the faster routine.
- Updated commit process to add support for alternate session archive content _in lieu of_ Omniplex PL2 file(s) when 
neural data is recorded: (1) Additional fields in the neural unit pickle file specifying SNR and template waveform for 
each identified unit. (2) A CSV file that contains the starting time in milliseconds for each Maestro trial saved
during the session. The format is "trial_filename.NNNN,timestamp_in_ms". Obviously the timestamps are in the same
"timeline" as the spike timestamps in the pickle file, so that the portal can determine which spikes occurred during
each trial.
- Added a description of the required session archive contents to the `README` for the `sglportalapi` Python package 
and to the `README` for the portal app itself.