# Offline opening (built 2026-11-04)
What it does: Home, Inspection Capture, Job Calendar and Service Records open with no signal. The pages, scripts, logos, login library and fonts are saved on the device by `sw.js`. Field Guides and SDS Sheets keep their own workers and are untouched.
Still needs signal: logging in, sending to the office, saving or moving a job, maps, weather. With no signal the Calendar shows the jobs it last saved on that device, read only ("Offline: showing jobs as of ..."). It is wiped on log out.
What is saved: home.html, index.html (Capture), calendar.html, records.html, lpc-config.js, lpc-clients.js, lpc-sw-banner.js, the manifest, the three Service Records data files, the logos, the pinned supabase-js file and the Google Fonts.
Updates: pages and scripts are fetched from the website first (4 second wait), and the saved copy is only used if the website is slow or unreachable. So a push reaches a device with signal on its next open, no steps needed.
If you change `sw.js` itself: devices show "A new version is ready. Tap to update." Only the tap switches over and reloads. Nothing ever reloads by itself.
Bump `VERSION` in `sw.js` only when you change sw.js, a logo, or the list of saved files. Not for a page or script change.
Check a device: Home's footer shows its build and "Opens offline (version)" once the pages are saved. The Calendar menu and Capture's Jobs sheet show their builds.
Switch it off: add `?nosw=1` to any page address (`home.html?nosw=1`). It clears the saved copy and stays off on that device until `?nosw=0`. In the iPad app (no address bar) use the "Switch off" link in Home's footer.
Preview on the Mac does nothing on file:// or localhost unless the address has `?sw=1`.
Test it on the iPad:
1. With signal, open Home from the Home Screen icon, then open each tool once (Calendar, Capture, Records).
2. Close the app, switch on airplane mode, reopen Home. Open each tool again: all four should open.
3. In the Calendar you should see "Offline: showing jobs as of ..." with today's jobs, no edit buttons.
4. Airplane mode off: the Calendar goes live again by itself (or tap Try again).
