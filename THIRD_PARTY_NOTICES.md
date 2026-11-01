# Third party notices

## Tesla vehicle command tools

The `tesla-control` and `tesla-keygen` programs offered on the Releases page are unmodified builds of
Tesla's open source vehicle command project:

* Source: https://github.com/teslamotors/vehicle-command (commit d6239bf377b8)
* Copyright Tesla, Inc.
* Licensed under the Apache License, Version 2.0: https://www.apache.org/licenses/LICENSE-2.0

They were cross compiled for 32 bit ARM (GOOS=linux, GOARCH=arm, GOARM=7) as static binaries. The full
licence text is included in the release download as LICENSE-vehicle-command.

SolarCharge runs these programs as separate tools; it does not include or modify their source code.

## Open Meteo

The solar forecast uses the free Open Meteo API (https://open-meteo.com), with data licensed under
CC BY 4.0.
