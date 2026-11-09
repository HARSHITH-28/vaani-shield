# VaaniShield Android companion

This project captures notification previews only from Google Messages (`com.google.android.apps.messaging`). It does not read the SMS database. A preview can be absent, private, incomplete, grouped, or suppressed while its conversation is open; background restrictions can delay delivery.

## Backend contract used

- `POST /api/v1/devices/pair`: `{pairing_code, device_id, device_name, device_model, android_version, app_version}`; response includes one-time `device_token`.
- `POST /api/v1/devices/heartbeat`: `{app_version}` and Bearer token; response includes `forwarding_permitted`, `last_seen_at`, and `server_time`.
- `POST /api/v1/devices/sms/ingest`: `{source_package, sender, sender_type, message_text, message_timestamp, client_dedup_key}` and Bearer token.
- Pairing errors include 400 (expired), 401 (invalid), 409 (reused/conflict), and 429 (rate limit). Device calls return 401 for invalid credentials, 403 for paused/revoked device, and 422 for invalid input.
- Timestamps are ISO-8601. Message timestamps are sent as UTC. The backend accepts `sender_type=unknown` and requires a nonempty sender, so unknown senders are marked `unknown`.

## Implementation notes

The service filters the Google Messages package and extracts the latest `MessagingStyle` message text, timestamp, and actual `Person` sender metadata. It falls back to `EXTRA_BIG_TEXT` / `EXTRA_TEXT`; it does not use a conversation title as the sender. A SHA-256 key based on notification key, timestamp, sender, and content is stored with the queued event, so retries reuse the same key. Room keeps up to 1,000 pending events. WorkManager checks the heartbeat, submits up to 20 queued events per run, retries temporary network/429/5xx failures with exponential backoff, and leaves permanent failures visible in diagnostics.

The device token is encrypted with AES-GCM using a non-exportable Android Keystore key. Preferences contain ciphertext and IV only. If the key is invalidated, the local token is cleared and pairing is required again. Message previews are stored in the local Room queue until delivery.

Debug builds accept HTTP only for loopback or RFC1918 private IPv4 addresses and produce a visible warning. Release builds require HTTPS. Never port-forward the backend or expose it to the public internet for testing.

## Open and build

Open `android-companion/` in Android Studio. The project targets compile/target SDK 35 and Java 17, with Android Gradle Plugin 8.7.3 and Kotlin 2.0.21. Allow Android Studio to install the required SDK/platform/build tools and download Gradle dependencies. Then choose **Build > Make Project**. Install on a connected phone with **Run** or **Build > Build Bundle(s) / APK(s) > Build APK(s)**. The debug APK, if built, will be at `app/build/outputs/apk/debug/app-debug.apk`.

The development machine inspected for this implementation did not have Java, Gradle, or Android SDK tools available, so no APK build was performed here. The Gradle and dependency versions are pinned project configuration, not detected installed versions.

## Windows private-LAN testing

1. Connect the Windows laptop and phone to the same trusted Wi-Fi network.
2. Find the laptop's Wi-Fi IPv4 address with `ipconfig`; use the address shown on the Wi-Fi adapter, not an assumed address.
3. Start the existing FastAPI backend bound to `0.0.0.0` on its configured development port. Keep it on the private network only.
4. If Windows Firewall blocks it, add an inbound rule for that port restricted to the **Private** profile and local subnet. Do not enable a public-profile rule or router port forwarding.
5. In the app, enter `http://<laptop-wifi-ip>:<port>` and pair. The app warns that HTTP is unencrypted.

## OnePlus Nord 3 / Android 14 notification access

Install and open VaaniShield, tap **Open Notification Access settings**, select **VaaniShield notification capture**, and enable **Allow notification access**. Confirm the system prompt, return to VaaniShield, and verify the status says **Allowed**. OxygenOS menu wording can vary; if the shortcut does not open, go to **Settings > Apps > Special app access > Device & app notifications** (or **Notification access**) and enable VaaniShield. Then pair, and turn on **Enable forwarding** only after confirming the configured server.

To stop forwarding, turn the switch off. To revoke server-side access, use the VaaniShield website's paired-device management; clearing local credentials alone cannot revoke a token at the server.
