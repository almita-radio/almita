# ALMITA empirical mount slew model

`mount_slew_model.py` predicts central and conservative slew durations from the current and target equatorial coordinates, observation time, and observer location. It is hardware-independent and does not issue GOTO commands.

The frozen MODEL-04 was trained on 132 successful physical movements. Normal movements use `13.0858 + 0.322017 × axis_max_deg`; geometric HA-zero-crossing movements use 64.1933 seconds. The safe predictions add 14.2204 seconds for normal movements and use 85.4585 seconds for crossings.

`axis_max_deg` is the maximum of the absolute wrapped RA displacement (hours converted to degrees) and absolute DEC displacement. `ha_zero_crossing` means only that start HA and target HA have opposite signs. It does not imply pier side, a meridian flip, or any internal mount mechanism.

Stratified five-fold CV produced global MAE 7.06 s, RMSE 9.66 s, R² 0.8355, and 95.45% safe coverage. Safe coverage was 95.29% for normal movements and 95.74% for HA-zero-crossing movements.

The model is empirical and site/dataset-specific. Predictions outside the observed axis, angular-distance, or HA ranges set `extrapolation_warning=True`; callers should apply their own conservative policy. Coefficients remain frozen until sufficient new physical evidence supports a revision.
