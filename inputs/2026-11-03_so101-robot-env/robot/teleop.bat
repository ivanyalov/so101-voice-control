@echo off
REM SO-101 teleoperation: leader (COM8) drives follower (COM9).
REM max_relative_target=5 limits each step to 5 degrees (safety for the first runs).
cd /d "%~dp0"
lerobot-teleoperate --robot.type=so101_follower --robot.port=COM9 --robot.id=so101_follower --teleop.type=so101_leader --teleop.port=COM8 --teleop.id=so101_leader --robot.max_relative_target=5
pause
