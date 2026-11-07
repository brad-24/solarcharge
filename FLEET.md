# Tesla API backup (optional)

Tesla's own Fleet API can carry SolarCharge's commands whenever Bluetooth can't get through, for example when phone keys have filled the car's Bluetooth slots. SolarCharge then tries, in order:

1. **Bluetooth**: free and local
2. **Tesla API**: about US$0.001 a command, and a wake about US$0.02. Every Tesla account gets US$10 of use a month, so normal use costs nothing
3. **Teslemetry**, if set up: credits, and only for starts, stops and lowering the amps

The setup is a one time job of about an hour. You need a free GitHub account (or any website you control) to publish a public key.

## 1. Publish a website for your key

Tesla checks that you control a web address. A free GitHub Pages site works:

1. On GitHub, create a **public** repository named `<your username>.github.io`
2. Add an empty file named `.nojekyll` (without it, GitHub Pages hides folders that start with a dot)
3. Wait a minute, then check `https://<your username>.github.io` opens (a 404 page is fine at this point)

## 2. Make SolarCharge's key

On the Pi:

```
python3 ~/solarcharge/solarcharge.py --fleet-keygen
```

This creates a private key at `~/.solarcharge_fleet_key.pem` (it never leaves the Pi) and prints the public key. In your GitHub repository, add a file at exactly this path and paste the public key into it:

```
.well-known/appspecific/com.tesla.3p.public-key.pem
```

Check it opens at `https://<your username>.github.io/.well-known/appspecific/com.tesla.3p.public-key.pem`.

## 3. Create a Tesla developer app

1. Sign in at [developer.tesla.com](https://developer.tesla.com) with your Tesla account (it needs multi factor authentication turned on)
2. Create a new application. Give it a unique name and say it is for personal solar charging of your own car
3. OAuth grant type: **Authorization Code and Machine to Machine**
4. Allowed origin: `https://<your username>.github.io`
5. Redirect URI: `https://<your username>.github.io/callback`
6. Scopes: **Vehicle Information**, **Vehicle Commands** and **Vehicle Charging Management**
7. If it asks for billing details, add them. The monthly allowance covers SolarCharge
8. Note the **Client ID** and **Client Secret**

## 4. Tell SolarCharge

In `~/solarcharge/config.ini` add:

```
[fleet]
enabled = false
client_id = <Client ID>
client_secret = <Client Secret>
domain = <your username>.github.io
```

Leave `enabled = false` until the last step. Outside North America and Asia Pacific, also set `api_base` (see config.example.ini).

## 5. Register and sign in

```
python3 ~/solarcharge/solarcharge.py --fleet-register
python3 ~/solarcharge/solarcharge.py --fleet-login
```

`--fleet-register` checks your published key matches and registers your domain with Tesla.

`--fleet-login` prints a link. Open it in any browser, sign in to Tesla and allow access. You end up on a "page not found" at your GitHub address: that's expected. Copy the whole address from the address bar and paste it into the Pi. The login is saved privately at `~/.solarcharge_fleet.json` and SolarCharge renews it by itself.

## 6. Add the key to the car

On your phone, open `https://tesla.com/_ak/<your username>.github.io` and approve the key in the Tesla app. The car needs to be awake and online. Your domain then appears in the car's key list under Controls, Locks.

## 7. Turn it on

Set `enabled = true` under `[fleet]`, then:

```
python3 ~/solarcharge/solarcharge.py --check
sudo systemctl restart solarcharge
```

The check should say `Tesla API OK logged in`. In the events, commands sent this way end in "via Tesla API".

Keep `~/.solarcharge_fleet_key.pem`, `~/.solarcharge_fleet.json` and your Client Secret private. If you don't use SolarCharge for over three months the login expires: run `--fleet-login` again.
