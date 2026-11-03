# SO-101 Robot Arm — Environment Package

This package is the working setup for our **SO-101 leader/follower pair**: the driving library
(lerobot), servo utilities, our calibration files, and a minimal example that drives the follower
from Python.

You own the voice side. Once the interface is wired up, every voice command ends up as a
`send_action()` call on the layer described in section 3.

---

## 1. What you need to install

| Item | Requirement | Notes |
|---|---|---|
| **Python** | **3.11 or 3.12** | Do not use 3.13+ (some dependencies have no wheels yet) |
| **USB driver** | CH343 serial driver | The controller boards use the CH343 chip. Windows 10/11 usually works out of the box — if you see `COMx` ports in Device Manager, you are fine |
| **GPU** | Optional | lerobot runs on CPU; you only need a GPU for policy training |
| **Disk** | ~8 GB | torch is the bulk of it |

### Two USB ports = two arms

The leader and the follower each have their own controller board, so plugging both in gives you
**two serial ports**. To identify which is which:

```bash
lerobot-find-port        # lists all ports, asks you to unplug one, then tells you its port
```

Our setup (port numbers can change after replugging — match on the **board serial number**):

| Arm | Port | Serial | Purpose |
|---|---|---|---|
| Leader (with trigger handle) | `COM8` | 5B91027464 | You move it by hand to teleoperate |
| Follower (with gripper) | `COM9` | 5B91026201 | The arm that actually executes |

> ⚠️ **Both controller boards need their own DC power supply.** With USB only, the servo bus is
> unpowered and every scan times out (`comm=-6`, or lerobot reports `Full found motor list: {}`).
> This is the single most common failure.

---

## 2. Install (one command)

**Windows:** double-click `setup_windows.bat`
**macOS / Linux:** `chmod +x setup_unix.sh && ./setup_unix.sh`

The script creates `.venv`, installs `requirements-lock.txt`, and copies `calibration/` into
`~/.cache/huggingface/lerobot/calibration/`.

Pinned versions: `lerobot 0.4.4`, `feetech-servo-sdk 1.0.0`, `torch 2.10.0`, `numpy 2.2.6`.

> torch installs the CUDA build by default on Windows. If you have no NVIDIA GPU and want to save
> ~3 GB: `pip install torch==2.10.0 torchvision==0.25.0 --index-url https://download.pytorch.org/whl/cpu`

---

## 3. Verify the setup

```bash
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 1) Read-only scan: are all six servos present with the right IDs?
python robot/scan_motors.py COM9          # use /dev/ttyACM0 on macOS/Linux

# 2) Minimal drive example: connect -> print joint angles -> hold 2 s -> open/close gripper -> disconnect
python robot/drive_arm_demo.py COM9
```

`drive_arm_demo.py` shows **the exact shape you should target**:

```python
robot = SO101Follower(SO101FollowerConfig(port="COM9", id="so101_follower", max_relative_target=5))
robot.connect()                          # loads the calibration file
obs = robot.get_observation()            # {"shoulder_pan.pos": 12.3, ...} in degrees
robot.send_action({                      # target angles in degrees
    "shoulder_pan.pos": 0.0,
    "shoulder_lift.pos": -20.0,
    "elbow_flex.pos": 30.0,
    "wrist_flex.pos": 0.0,
    "wrist_roll.pos": 0.0,
    "gripper.pos": 50.0,                 # 0 = closed, 100 = open
})
robot.disconnect()                       # torque is released on exit
```

**Please add a rate limit in the middle-platform layer** so an LLM/VLA output can never produce a
large jump:

```python
SO101FollowerConfig(..., max_relative_target=5)   # at most 5 degrees per step
```

---

## 4. Manual teleoperation (no code, good for checking hardware)

```bash
lerobot-teleoperate --robot.type=so101_follower --robot.port=COM9 --robot.id=so101_follower \
  --teleop.type=so101_leader --teleop.port=COM8 --teleop.id=so101_leader \
  --robot.max_relative_target=5
```

On Windows just run `robot/teleop.bat` (ports are hard-coded — edit them if yours differ).

Before starting, **pose the follower close to the leader's current pose**. Teleoperation is an
absolute mapping, so a large initial difference makes the follower snap violently.

---

## 5. What is in the package

```
so101-robot-env/
├─ setup_windows.bat / setup_unix.sh   # one-command install
├─ requirements-lock.txt               # pinned versions
├─ calibration/                        # calibration for our arms
│  ├─ so101_follower.json
│  └─ so101_leader.json
└─ robot/
   ├─ drive_arm_demo.py    # ★ Python driving example (the integration entry point)
   ├─ scan_motors.py       # read-only bus scan
   ├─ set_motor_id.py      # assign an ID to a single servo
   ├─ teleop.bat           # leader/follower teleoperation
   └─ so101_docs.mdx       # official SO-101 docs, offline copy
```

---

## 6. Hardware configuration (our build)

- Servos: STS3215, model number `777`, **baudrate 1 Mbps**, protocol 0
- ID mapping (identical on both arms):

  | ID | Joint | Notes |
  |---|---|---|
  | 1 | shoulder_pan | base rotation |
  | 2 | shoulder_lift | upper arm pitch |
  | 3 | elbow_flex | elbow |
  | 4 | wrist_flex | wrist pitch |
  | 5 | wrist_roll | wrist roll (full turn) |
  | 6 | gripper | follower = gripper, leader = trigger |

- Wiring: daisy-chain in ID order 1 → 6; the controller board connects to the **id=1** end
- Firmware: follower all 3.9, leader all 3.10. **lerobot requires identical firmware on one bus** —
  a mixed set is rejected with `Some Motors use different firmware versions`

---

## 7. Calibration (mandatory on different hardware)

Calibration is **per physical servo** (each one has its own homing offset and travel range), so a
different arm needs a new calibration:

```bash
# follower
lerobot-calibrate --robot.type=so101_follower --robot.port=COM9 --robot.id=so101_follower
# leader
lerobot-calibrate --teleop.type=so101_leader --teleop.port=COM8 --teleop.id=so101_leader
```

Procedure: move to the middle of each joint's range → Enter → (torque is released, hold the arm) →
sweep every joint to both limits → Enter.

**Gotcha:** if a calibration file already exists you get
`Press ENTER to use provided calibration file...`. Enter reuses the old one — you must type **`c`**
and Enter to actually recalibrate.

---

## 8. Other pitfalls we already hit

1. **Do not use `lerobot-setup-motors`** to number a single servo — it always walks
   gripper → … → pan and will keep renaming the *same* motor to 6, 5, 4… Use
   `robot/set_motor_id.py <port> <joint>` instead.
2. If a calibrated joint has an absurdly small span (e.g. `span=2`), that joint was never swept
   during calibration — just recalibrate.
3. `comm=-6` or an empty scan almost always means the controller board has **no DC power**.
4. When reinstalling lerobot, watch the **numpy version**: lerobot needs numpy ≥ 2. With numpy 1.x,
   `pyarrow` fails to import and every `lerobot-*` command crashes on startup.
