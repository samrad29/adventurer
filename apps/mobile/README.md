# Test native authentication locally

Start the isolated PostgreSQL test database on port 54329. From the repo root, apply the migrations and run Flask against that database. The command supplies the local connection directly to the app factory.

```powershell
$env:TEST_DATABASE_URL = 'postgresql://postgres:postgres@127.0.0.1:54329/adventurer_migration_test'
.venv/Scripts/python.exe -m alembic upgrade head
.venv/Scripts/python.exe -c 'import os; from adventurer import create_app; create_app(dict(DATABASE_URL=os.environ["TEST_DATABASE_URL"], SECRET_KEY="local-device-testing", INVITE_CODE="")).run(host="0.0.0.0", port=5000)'
```

Keep pytest and the device session separate because the tests clear the isolated database. For invite-code testing, set the factory's `INVITE_CODE` to a local test value.

In this directory, copy `.env.example` to `.env.local` and set `EXPO_PUBLIC_API_BASE_URL`. Use `http://10.0.2.2:5000` for the standard Android emulator or `http://127.0.0.1:5000` for an iOS simulator running on the Flask host. For a physical device, use the Flask computer's LAN IP and keep both devices on the same trusted network. If the device cannot reach Flask, check local firewall access and open `/health` from the device. If a native build blocks cleartext HTTP, use a trusted HTTPS development endpoint that forwards to the isolated Flask app.

Run `npm ci` and `npx expo start`, then open the app in the SDK-compatible Expo Go client or an existing development build. Restart Expo after changing the API origin and sign out before switching origins.

Expo generates typed route declarations on startup. For a standalone typecheck, run `npx expo customize tsconfig.json` first, then `npm run typecheck`.

1. Register, check that home loads, and quit and relaunch the app to verify session restoration.
2. With a saved session, stop Flask and relaunch. Verify that the app offers a retry without showing private data. Restart Flask and retry.
3. Sign out, then sign into a different account and check that home shows the new account. Try signing out while offline. The device should sign out and report that server revocation could not be confirmed.
4. Revoke the local test account's tokens with `POST /api/v1/auth/revoke` from another test session. Reload the app and verify it returns to sign-in.
