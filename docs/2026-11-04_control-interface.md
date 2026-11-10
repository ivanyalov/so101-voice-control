# Local browser control

Run `./control` and open the loopback URL printed in the terminal. Opening the page does not contact the robot or open the microphone. For a demo that never calls the backend, run `./control --demo`.

The page has a command history, typed input and click-to-record voice input. Click **Record voice**, speak, then click **Finish recording**. Capture ends automatically after 30 seconds. The Mac transcribes the audio locally, and Grok receives the transcript. The same configuration and host requirements as the terminal apply. See the [README](../README.md).

**Preview movement** is on initially. Spoken and typed natural-language previews use the same target preparation as execution. Taught base buttons also require the matching local direction profile. Numeric jog and move previews only show the requested command. Legacy numeric move, jog and Start control are unavailable with the native host. Use natural-language commands for managed movement.

**Stop**, **Read joints**, **Check connection**, and **Freedrive** are direct actions regardless of the preview setting. Read joints returns a snapshot. Freedrive releases torque, so support the arm first. Stop cancels UI-owned work and requests a driver stop. A target already sent can remain active until the driver handles that request.

Use one command-producing session at a time. Closing the browser tab does not stop a command. Ctrl+C closes the UI and its pending local work but does not send a driver stop. The server listens only on loopback and requires a random per-launch token for API requests. Keep the printed URL local.
