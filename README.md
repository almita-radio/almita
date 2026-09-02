<p align="center">
  <img src="almita-logo.png" alt="ALMITA logo" width="100%">
</p>

# ALMITA

**Antenna Listening Mostly to Interference, Tentatively Astronomy**

ALMITA is an amateur 21 cm neutral hydrogen radio telescope built in **Chile** by **Felipe Fridman G.** (ffridman@gmail.com)

The project combines accessible RF hardware, a Raspberry Pi 5, RTL-SDR receivers, INDI/OnStep mount control, automated sky planning, HDF5 acquisition, live quicklook products and a lightweight web console.

Being based in Chile was a lucky accident.

We just happened to build a radio telescope in a country with some of the best skies on Earth.

ALMITA is mostly vibe-coded, extensively field-tested, occasionally threatened with a hammer, and still held together in suspiciously many places by **plastic cable ties**.

This is considered temporary.

It has also been considered temporary for quite some time.

---

## What ALMITA does

ALMITA can:

- Generate physically consistent sky mosaics from beam width, sampling and angular field size.
- Plan observations in equatorial coordinates.
- Support rotated and Galactic observation geometries.
- Control an equatorial mount through **INDI + OnStep**.
- Acquire raw I/Q data from an **RTL-SDR Blog V4**.
- Store observations and metadata in **HDF5**.
- Record RA, Dec, altitude, azimuth, UTC time and observatory coordinates.
- Measure and store SDR and LNA temperatures.
- Keep SDR gain fixed during science observations.
- Generate live **Spectrum**, **Waterfall** and sky-map quicklooks.
- Monitor sessions through a local web console.
- Operate completely offline in the field.
- Preserve raw observations for future reprocessing.
- Use a secondary SDR as an independent RFI reference receiver.

ALMITA currently treats HI measurements as **relative/instrumental data**.

Absolute calibration in Kelvin is an active development goal, not a capability we pretend to already have.

---

## Hardware

The current setup includes:

- **Raspberry Pi 5**
- **RTL-SDR Blog V4** — primary science receiver
- **RTL-SDR Blog V3** — secondary RFI reference receiver
- **Nooelec Hydrogen LNA** — primary 1420 MHz science-chain LNA
- **RTL-SDR.com Wideband LNA (SPF5189Z-based)** — secondary RFI reference-chain LNA
- **Nooelec Flamingo FM** — FM broadcast notch filter for the RFI reference chain
- 1420 MHz feed
- Modified ~90 × 60 cm WiFi grid/parabolic reflector
- Equatorial mount
- **OnStep** mount controller
- **INDI**
- LNA and SDR temperature sensors
- 50 Ω RF reference loads
- Experimental 1420 MHz RFI-monitor dipole
- Ferrites, coaxial cable, SMA connectors
- More plastic cable ties than a serious observatory would probably admit to

The hardware is intentionally accessible, experimental and repairable.

It is not a commercial telescope kit.

---

## Software stack

ALMITA is mainly written in **Python** and runs locally on the Raspberry Pi.

Core technologies include:

- Linux
- Python
- NumPy
- Astropy
- h5py / HDF5
- INDI / PyINDI
- OnStep
- rtl_tcp
- Matplotlib
- systemd
- local web monitoring
- local HI sky reference data

Internet access is not required for observation.

---

## Observation workflow

A typical session looks like this:

1. Start mount and SDR services.
2. Generate an observation plan.
3. Verify visibility and geometry.
4. Perform RF preflight.
5. Confirm nominal SDR gain and headroom.
6. Start acquisition.
7. Move through the planned sky positions.
8. Store raw I/Q and metadata in HDF5.
9. Generate live quicklook products.
10. Monitor progress from the ALMITA Console.
11. Process and compare observations offline.

The science receiver remains the primary acquisition path.

Auxiliary monitoring must never block or interfere with the main capture.

---

## Grid Generator

ALMITA does not define a grid as “some number of points that looks nice”.

The observation geometry is derived from:

- beam width
- sampling factor
- angular field size
- sky position
- coordinate frame
- observation mode

The current design supports or is evolving toward:

- **EQUATORIAL_RECT**
- **EQUATORIAL_ROTATED**
- **GALACTIC_RECT**

Future modes include Galactic strips and scans for longitudinal and transverse HI surveys.

Galactic plans are intended to always produce multiple views:

- **Galactic coordinates** — to understand the science.
- **Equatorial coordinates** — to understand what the mount will execute.
- **Local observer sky projection** — to understand what the field actually looks like from the ground.

A local HI dataset may be used as a contextual sky background for planning.

One particularly important campaign has an unofficial scientific name:

**the little Milky Way arm survey.**

---

## Alignment

ALMITA includes an alignment workflow, but alignment is treated conservatively.

The system does **not** perform arbitrary mount synchronization just because a software model says it should.

A valid alignment requires a physically defensible reference.

The intended workflow is:

1. Select a known reference direction or source.
2. Compare expected and measured pointing.
3. Evaluate the angular offset.
4. Apply correction only when the reference is trustworthy.
5. Preserve the resulting alignment state in session metadata.

The goal is not simply to make the mount “look correct”.

The goal is to know **why** it is correct.

If no defensible reference is available, ALMITA prefers an honest pointing uncertainty over a fake precision.

---

## Antenna characterization

The antenna beam is currently represented by a configured approximate beam width.

This allows ALMITA to generate consistent mosaics, sampling patterns and coverage maps.

However, the final physical beam must eventually be measured.

A proper beam-characterization campaign will include:

- controlled angular sweeps
- a suitable reference source
- repeatable pointing
- measured signal versus angular offset
- estimation of the real beam profile
- measured FWHM
- sidelobe inspection
- uncertainty reporting

Until that campaign is completed, the configured beam value should be treated as an operational model, not a final metrological truth.

This matters because the beam determines:

- spatial resolution
- sampling density
- map smoothness
- overlap between observations
- interpretation of apparent source size

ALMITA would rather admit that the beam is provisional than claim twenty decimal places of precision from a mesh dish attached with cable ties.

---

## Gain characterization

ALMITA does **not** use AGC during science observations.

Instead, the intended strategy is to determine a **nominal operating gain** for the complete RF chain.

The question is:

> What gain gives the best useful sensitivity without approaching clipping, compression or non-linear behavior?

The characterization process will test several gain values while measuring:

- ADC headroom
- clipping
- RMS level
- noise floor
- RFI response
- stability
- repeatability

Once a nominal gain is established, normal sessions will use that same value by default.

Before each observation, ALMITA performs a short RF preflight.

If the nominal gain is safe:

- keep it fixed
- do not touch it
- observe the full mosaic

If strong RFI or saturation is detected, gain may be reduced before the session begins.

Once acquisition starts, gain remains fixed.

This improves:

- repeatability
- comparison between sessions
- relative calibration
- scientific honesty

Constantly changing gain in the middle of a sky map is considered bad manners.

---
## RFI reference receiver

ALMITA includes a second RTL-SDR dedicated to monitoring the local RF environment independently from the primary science receiver.

Current architecture:

MAIN
1420 MHz science feed
→ Nooelec Hydrogen LNA
→ RTL-SDR Blog V4
→ full science acquisition
→ HDF5

RFI_REF
1420 MHz dipole
→ Nooelec Flamingo FM notch filter
→ RTL-SDR.com Wideband LNA
→ RTL-SDR Blog V3
→ lightweight FFT monitoring
→ RFI diagnostics
