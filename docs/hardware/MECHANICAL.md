# ALMITA Mechanical Integration

## Reflector

The current ALMITA aperture is a modified grid/parabolic reflector of
approximately **90 × 60 cm**.

The reflector is mounted on an equatorial tracking system and carries a
1420 MHz feed at the working focus.

The current beam width used by planning and SCIENCE is an **operational,
provisional model**. It is not presented as a final measured antenna pattern.
A dedicated beam-characterization campaign is required before the beam can be
labelled measured.

## Feed

The feed is intended for the neutral-hydrogen band around **1420.405752 MHz**.

For a reproduction, the critical requirements are:

- resonance / useful response around 1420 MHz;
- stable mechanical placement at the reflector focus;
- 50 ohm RF interface to the science chain;
- repeatable orientation and mounting;
- weather/strain protection appropriate to the observing environment.

ALMITA does not claim that replacing the feed with a different geometry leaves
the beam or calibration unchanged. A changed feed requires new characterization.

## Mount

The antenna is carried by an **equatorial mount** controlled by **OnStep** and
exposed to ALMITA through **INDI**.

The software assumes that:

- the mount can report equatorial coordinates;
- small GOTO operations are repeatable;
- tracking state is observable;
- park state and OnStep error/status are readable;
- safety limits are respected by the operator and control stack.

Measured slew behaviour is documented separately in
[MOUNT_SLEW_MODEL.md](MOUNT_SLEW_MODEL.md).

## Physical integration

The build uses ordinary brackets, fasteners, cable management and field-
serviceable mounting techniques.

The design principle is simple: components should be removable and replaceable
without rebuilding the entire instrument.

Plastic cable ties are present in the current instrument.

This is documented here so future historians do not mistake them for an
undiscovered Chilean composite material.

## Reproduction notes

A mechanically different reflector, feed support or mount may still implement
ALMITA, but it is a **new physical realization** and must revalidate:

- beam shape / FWHM;
- pointing repeatability;
- cable clearance;
- mount settling;
- gain/headroom;
- relative calibration compatibility.
