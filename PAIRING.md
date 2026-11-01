# Bluetooth pairing (one time)

This lets the Pi send start, stop and amps commands straight to the car over Bluetooth, for free.
The key you create only gets the "charging manager" role: it can control charging and wake the car,
but it cannot unlock or drive it.

You need: the car parked within Bluetooth range of the Pi, your Tesla key card, and a way to run
commands on the Pi while you are sitting in the car (a phone SSH app such as Termius, or a laptop).

Install the tools first, as described under Install in the README.

In each command below, replace YOUR_VIN with your car's VIN. Setting it once makes that easy:

    VIN=YOUR_VIN

## 1. Create the Pi's key

    ~/solarcharge/bin/tesla-keygen -key-file ~/.solarcharge_ble_key.pem create > ~/solarcharge/ble-public.pem
    chmod 600 ~/.solarcharge_ble_key.pem

Never share ~/.solarcharge_ble_key.pem. The public file is harmless.

## 2. Check the Pi can talk to the car (no key needed yet)

    ~/solarcharge/bin/tesla-control -ble -vin $VIN body-controller-state

It should print a few lines including vehicleSleepStatus. If it says the device is busy or it can't
initialise the adapter, run this once and try again:

    sudo systemctl disable --now bluetooth

If it says the vehicle is already connected to the maximum number of Bluetooth devices, turn off
Bluetooth on nearby phones and watches that are paired as keys, then try again.

## 3. Pair (at the car)

Sit in the car with the key card. Then run:

    ~/solarcharge/bin/tesla-control -ble -vin $VIN add-key-request ~/solarcharge/ble-public.pem charging_manager cloud_key

The car's screen asks you to confirm the new key. Tap your key card on the card reader on the centre
console. The new key then appears under Controls, Locks.

## 4. Test it

    ~/solarcharge/bin/tesla-control -ble -vin $VIN -key-file ~/.solarcharge_ble_key.pem state charge | head -20

You should see the battery level and charge limit.

## 5. Switch SolarCharge over

In ~/solarcharge/config.ini, under [ble], set:

    enabled = true

Then restart and check:

    sudo systemctl restart solarcharge
    python3 ~/solarcharge/solarcharge.py --check

The check should show "Bluetooth OK". On the dashboard, Car commands should say Bluetooth.

To go back to Teslemetry at any time, set enabled = false and restart.
