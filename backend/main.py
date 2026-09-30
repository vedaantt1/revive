"""
Gamified Rehab Tracker - Pose Backend  (multi-exercise auto detection)
======================================================================
Watches the webcam, works out WHICH exercise you are doing, counts reps with
MediaPipe Pose, judges form (range of motion), and broadcasts every counted rep
as JSON over a websocket (ws://localhost:8765).

EXERCISES IT KNOWS (see the EXERCISES catalog below)
    bicep_curl      face the camera (or 3/4 on), arm hanging, curl up
    shoulder_press  face the camera, hands at shoulders, press overhead
    lateral_raise   face the camera, straight arms out to the sides
    squat           stand SIDE-ON to the camera, feet together, both knees bend
    lunge           stand SIDE-ON, split stance (one foot well in front), both feet stay down
    knee_raise      face the camera, march / lift one knee towards the hip
    push_up         camera low, SIDE-ON to your body (plank position)
    Tip: full body in frame, 2-3 m back, decent light. Start each exercise from the
    resting position (standing straight / arms down / plank arms straight).

INSTALL (PowerShell, Python 3.10-3.12):
    py -m pip install mediapipe opencv-python numpy websockets
    (if `mp.solutions` errors on a very new mediapipe: py -m pip install mediapipe==0.10.14)

RUN:
    py main.py
    Keys in the video window:  ESC = quit,   R = forget current exercise and re-detect

HOW AUTO-DETECTION WORKS
    Every exercise is one "signal" per body side (a joint angle, or a height ratio) that
    is HIGH at rest and drops while you do the movement, plus a "gate": a posture check
    that says "this really looks like that exercise" (e.g. torso upright, both feet on the
    floor, upper arm hanging). Each candidate arms itself once its signal has been seen at
    rest, then triggers when the signal drops by a clear amount with its gate passing for a
    few frames in a row. The strongest trigger wins, and locks the exercise AND the body
    side. Then reps are counted and form is checked for that exercise. Detection keeps
    running while locked, so starting a different exercise switches to it automatically.
    After ~5 s of not moving it also goes back to detecting (or press R to force it).
    Detecting: the panel on the right shows each exercise's signal, A = armed, G = gate ok.
    Use it to see why something isn't triggering, then tweak the catalog numbers.

TEST THE WEBSOCKET:
    - wscat:      npx wscat -c ws://localhost:8765
    - dashboard:  point the teammate dashboard at ws://localhost:8765
    - no camera?  py replay.py  (same port, fake events every 3 s; run only one at a time)

EVENT FORMAT (fixed contract, one JSON string per counted rep, sent to all clients):
    {"event": "rep_counted", "reps": 7, "depth_ok": true, "form_score": 85.5,
     "exercise": "squat", "state": "standing"}
    "exercise" is the detected exercise name (squat, lunge, knee_raise, bicep_curl,
    shoulder_press, lateral_raise, push_up). depth_ok = the rep reached full range.

ADD AN EXERCISE: write a measure (signal, HIGH at rest) and a gate below, then add one
entry to EXERCISES. Nothing else needs to change.
"""

import asyncio
import json
import math
import sys
import threading
import time
from collections import deque

import cv2
import mediapipe as mp
import numpy as np
import websockets

# =============================================================================
# CONFIG - all tuning lives here (per-exercise thresholds are in EXERCISES below)
# =============================================================================
HOST = "localhost"
PORT = 8765
# "auto" = listen for every exercise in the catalog. Or force one: "squat".
# Or restrict the list: ("squat", "bicep_curl", "lateral_raise")  <- fewer = more reliable demo
EXERCISE = "auto"

CAMERA_INDEX = 0
FRAME_WIDTH = 640           # small frames = faster on CPU
FRAME_HEIGHT = 480
MIRROR_VIEW = True          # flip the preview like a mirror (more natural for the person)
MODEL_COMPLEXITY = 1        # 0 = fastest, 1 = default, 2 = most accurate. Drop to 0 if laggy.
MIN_DETECTION_CONF = 0.5
MIN_TRACKING_CONF = 0.5
<<<<<<< HEAD
MIN_VISIBILITY = 0.4        # ignore a landmark when MediaPipe is less sure than this
SIGNAL_SMOOTHING = 0.5      # EMA weight of the newest value (1.0 = no smoothing, lower = smoother)
DEBUG_HINTS = True             # show "hips/feet not in frame" hints on screen
=======
MIN_VISIBILITY = 0.5        # ignore a landmark when MediaPipe is less sure than this
SIGNAL_SMOOTHING = 0.5      # EMA weight of the newest value (1.0 = no smoothing, lower = smoother)
>>>>>>> 141742bef4e9f741f884417b472b6c97997cbeb6
FEEDBACK_SECONDS = 1.5      # how long the GOOD/SHALLOW banner stays on screen

# --- auto-detection tuning ---
DETECT_WINDOW_SECONDS = 3.0   # how far back we look for the "drop" from the resting value
DETECT_CONFIRM_FRAMES = 4     # frames in a row the same exercise must lead before we lock in
SWITCH_IDLE_SECONDS = 5.0     # not moving this long (between reps) -> detect again
ACTIVE_TIMEOUT_SECONDS = 10.0 # stuck mid-rep this long -> abandon that rep and detect again

# reps across exercises: False = one cumulative counter for the whole run (reps never go
# backwards, matches "cumulative count this run"); True = separate count + form % per exercise
PER_EXERCISE_COUNTS = False

# --- posture gates (used by the gate functions below) ---
TORSO_UPRIGHT_MAX_TILT = 35       # deg from vertical: torso counts as upright below this
TORSO_HORIZONTAL_MIN_TILT = 55    # deg from vertical: torso counts as horizontal (push-up) above this
LUNGE_STAGGER_MIN = 0.7           # horizontal gap between ankles / torso length: >= lunge, < squat
FOOT_LIFT_MIN = 0.15              # ankle height difference / torso length: above = one foot lifted
<<<<<<< HEAD
CURL_UPPER_ARM_MAX = 50           # deg: upper arm hanging (hip-shoulder-elbow angle) for a curl
PRESS_UPPER_ARM_MIN = 20          # deg: upper arm raised away from the body for a press
=======
CURL_UPPER_ARM_MAX = 40           # deg: upper arm hanging (hip-shoulder-elbow angle) for a curl
PRESS_UPPER_ARM_MIN = 30          # deg: upper arm raised away from the body for a press
>>>>>>> 141742bef4e9f741f884417b472b6c97997cbeb6
STRAIGHT_ARM_MIN = 140            # deg: elbow angle that counts as a straight arm (lateral raise)
# =============================================================================

# MediaPipe landmark indices: (left, right)
LM = {
    "shoulder": (11, 12), "elbow": (13, 14), "wrist": (15, 16),
    "hip": (23, 24), "knee": (25, 26), "ankle": (27, 28),
}
SIDES = ("left", "right")


def other_side(side):
    return "right" if side == "left" else "left"


# -----------------------------------------------------------------------------
# Angle math + per-frame pose helper
# -----------------------------------------------------------------------------
def calc_angle(a, b, c):
    """Angle in degrees at point b formed by points a-b-c (each an (x, y) pair)."""
    a, b, c = np.array(a, float), np.array(b, float), np.array(c, float)
    ba, bc = a - b, c - b
    denom = np.linalg.norm(ba) * np.linalg.norm(bc)
    if denom < 1e-6:
        return None
    cos = np.clip(np.dot(ba, bc) / denom, -1.0, 1.0)
    return float(np.degrees(np.arccos(cos)))


class PoseFrame:
    """Wraps one frame of landmarks. Every method returns None if it needs a landmark
    that isn't visible, so measures and gates just propagate 'unknown'."""

    def __init__(self, lm, w, h):
        self.lm, self.w, self.h = lm, w, h
        self._pts = {}

    def pt(self, part, side):
        key = (part, side)
        if key not in self._pts:
            v = None
            if self.lm is not None:
                p = self.lm[LM[part][SIDES.index(side)]]
<<<<<<< HEAD
                # skip low-confidence points AND points MediaPipe 'guesses' outside the frame
                if p.visibility >= MIN_VISIBILITY and -0.08 <= p.x <= 1.08 and -0.08 <= p.y <= 1.08:
=======
                if p.visibility >= MIN_VISIBILITY:
>>>>>>> 141742bef4e9f741f884417b472b6c97997cbeb6
                    # pixel coords (not normalized) so angles aren't distorted by aspect ratio
                    v = (p.x * self.w, p.y * self.h)
            self._pts[key] = v
        return self._pts[key]

    def angle(self, side, a, b, c):
        pa, pb, pc = self.pt(a, side), self.pt(b, side), self.pt(c, side)
        if pa is None or pb is None or pc is None:
            return None
        return calc_angle(pa, pb, pc)

    def knee_angle(self, side):
        return self.angle(side, "hip", "knee", "ankle")

    def elbow_angle(self, side):
        return self.angle(side, "shoulder", "elbow", "wrist")

    def shoulder_angle(self, side):
<<<<<<< HEAD
        """Upper arm vs torso: ~10 deg arm hanging, ~90 deg arm out to the side.
        If the hips are out of frame (close to the camera) it measures against straight
        down instead, so arm exercises still work with only the upper body visible."""
        a = self.angle(side, "hip", "shoulder", "elbow")
        if a is not None:
            return a
        sh, el = self.pt("shoulder", side), self.pt("elbow", side)
        if sh is None or el is None:
            return None
        return calc_angle((sh[0], sh[1] + 100.0), sh, el)
=======
        """Upper arm vs torso: ~10 deg arm hanging, ~90 deg arm out to the side."""
        return self.angle(side, "hip", "shoulder", "elbow")
>>>>>>> 141742bef4e9f741f884417b472b6c97997cbeb6

    def torso_vec(self):
        """Average hip->shoulder vector over the sides where both are visible."""
        vecs = []
        for s in SIDES:
            sh, hp = self.pt("shoulder", s), self.pt("hip", s)
            if sh is not None and hp is not None:
                vecs.append((sh[0] - hp[0], sh[1] - hp[1]))
        if not vecs:
            return None
        return (sum(v[0] for v in vecs) / len(vecs), sum(v[1] for v in vecs) / len(vecs))

    def torso_len(self):
        v = self.torso_vec()
        return None if v is None else math.hypot(v[0], v[1])

    def torso_tilt(self):
        """Degrees from vertical: 0 = upright, 90 = horizontal (plank)."""
        v = self.torso_vec()
        return None if v is None else math.degrees(math.atan2(abs(v[0]), abs(v[1])))

    def _ankles(self):
        return self.pt("ankle", "left"), self.pt("ankle", "right"), self.torso_len()

    def feet_gap(self):
        """Horizontal distance between ankles, in torso lengths (stagger of the stance)."""
        l, r, L = self._ankles()
        return None if l is None or r is None or not L else abs(l[0] - r[0]) / L

    def feet_level(self):
        """Vertical difference between ankles, in torso lengths (0 = both feet on the floor)."""
        l, r, L = self._ankles()
        return None if l is None or r is None or not L else abs(l[1] - r[1]) / L

    def foot_lift(self, side):
        """How much higher this ankle is than the other one, in torso lengths."""
        me, ot = self.pt("ankle", side), self.pt("ankle", other_side(side))
        L = self.torso_len()
        return None if me is None or ot is None or not L else (ot[1] - me[1]) / L


# -----------------------------------------------------------------------------
# MEASURES: one number per (exercise, side), HIGH at rest, dropping during the movement.
# Return None when the needed landmarks aren't visible.
# -----------------------------------------------------------------------------
def m_knee_angle(P, s):            # 172 straight -> ~90 deep
    return P.knee_angle(s)


def m_elbow_angle(P, s):           # 165 straight -> ~40 curled
    return P.elbow_angle(s)


def m_elbow_extension(P, s):       # 180 - elbow angle: bent at shoulders ~90 -> locked out ~10
    a = P.elbow_angle(s)
    return None if a is None else 180 - a


def m_arm_raise(P, s):             # 180 - upper-arm angle: arm down ~165 -> arm out level ~90
    a = P.shoulder_angle(s)
    return None if a is None else 180 - a


def m_knee_height(P, s):           # (knee below hip) / torso: standing ~0.8 -> knee at hip height ~0
    k, hp, L = P.pt("knee", s), P.pt("hip", s), P.torso_len()
    if k is None or hp is None or not L:
        return None
    return (k[1] - hp[1]) / L


# -----------------------------------------------------------------------------
# GATES: "does the posture look like this exercise?"
#   hold = posture that is true for the WHOLE movement incl. rest. If it stops being true the
#          candidate is disarmed (so a stale "arm was down" reading can't fire later).
#          Defaults to the gate when an exercise has no separate "hold".
#   gate = checked at the moment a candidate triggers (hold + anything only true mid-movement).
# -----------------------------------------------------------------------------
def _upright(P):
    t = P.torso_tilt()
<<<<<<< HEAD
    if t is None:      # hips not visible (upper-body-only frame): assume upright if a shoulder is seen
        return P.pt("shoulder", "left") is not None or P.pt("shoulder", "right") is not None
    return t <= TORSO_UPRIGHT_MAX_TILT
=======
    return t is not None and t <= TORSO_UPRIGHT_MAX_TILT
>>>>>>> 141742bef4e9f741f884417b472b6c97997cbeb6


def h_upright(P, s):               # hold check: torso upright
    return _upright(P)


def h_squat(P, s):                 # hold check: upright, feet together and both on the floor
    if not _upright(P):
        return False
    gap, lvl = P.feet_gap(), P.feet_level()
    if gap is not None and gap >= LUNGE_STAGGER_MIN:
        return False
    if lvl is not None and lvl > FOOT_LIFT_MIN:
        return False
    return True


def g_squat(P, s):                 # trigger check: posture above AND the other knee bending too
    if not h_squat(P, s):
        return False
    other = P.knee_angle(other_side(s))
    return other is None or other < 155


def g_lunge(P, s):                 # upright, split stance, both feet on the floor
    if not _upright(P):
        return False
    gap, lvl = P.feet_gap(), P.feet_level()
    if gap is None or gap < LUNGE_STAGGER_MIN:
        return False
    return lvl is None or lvl <= FOOT_LIFT_MIN


def g_knee_raise(P, s):            # upright and this foot is off the floor
    if not _upright(P):
        return False
    lift = P.foot_lift(s)
    return lift is not None and lift > FOOT_LIFT_MIN


def g_curl(P, s):                  # upright, upper arm hanging by the side
    if not _upright(P):
        return False
    a = P.shoulder_angle(s)
    return a is not None and a <= CURL_UPPER_ARM_MAX


def g_press(P, s):                 # upright, elbow raised away from the body, wrist above elbow
    if not _upright(P):
        return False
    a, w, e = P.shoulder_angle(s), P.pt("wrist", s), P.pt("elbow", s)
    return a is not None and a >= PRESS_UPPER_ARM_MIN and w is not None and e is not None and w[1] < e[1]


def g_lateral(P, s):               # upright, arm straight
    if not _upright(P):
        return False
    e = P.elbow_angle(s)
    return e is not None and e >= STRAIGHT_ARM_MIN


def g_push_up(P, s):               # body roughly horizontal
    t = P.torso_tilt()
    return t is not None and t >= TORSO_HORIZONTAL_MIN_TILT


# =============================================================================
# EXERCISE CATALOG
# Rep flow (same for every exercise; signal is HIGH at rest):
#   rest_state --(signal < enter_below)--> active_state --(signal > exit_above)--> REP COUNTED
#   rep is "good" (depth_ok) only if the lowest signal reached was < good_below.
#   min_drop = how far the signal must fall (in its own units) for auto-detection to trigger.
#   joints   = landmarks highlighted on screen when this exercise is locked in.
# =============================================================================
EXERCISES = {
    "squat": {
        "measure": m_knee_angle, "gate": g_squat, "hold": h_squat, "joints": ("hip", "knee", "ankle"),
        "rest_state": "standing", "active_state": "down",
        "enter_below": 140, "exit_above": 160, "good_below": 100, "min_drop": 20,
        "shallow_msg": "go deeper",
    },
    "lunge": {
<<<<<<< HEAD
        "measure": m_knee_angle, "gate": g_lunge, "hold": h_upright, "joints": ("hip", "knee", "ankle"),
=======
        "measure": m_knee_angle, "gate": g_lunge, "joints": ("hip", "knee", "ankle"),
>>>>>>> 141742bef4e9f741f884417b472b6c97997cbeb6
        "rest_state": "standing", "active_state": "lunging",
        "enter_below": 140, "exit_above": 160, "good_below": 105, "min_drop": 20,
        "shallow_msg": "sink lower",
    },
    "knee_raise": {
        "measure": m_knee_height, "gate": g_knee_raise, "hold": h_upright, "joints": ("hip", "knee", "ankle"),
        "rest_state": "standing", "active_state": "lifting",
        "enter_below": 0.45, "exit_above": 0.60, "good_below": 0.20, "min_drop": 0.20,
        "shallow_msg": "lift knee to hip height",
    },
    "bicep_curl": {
        "measure": m_elbow_angle, "gate": g_curl, "joints": ("shoulder", "elbow", "wrist"),
        "rest_state": "extended", "active_state": "curling",
<<<<<<< HEAD
        "enter_below": 120, "exit_above": 140, "good_below": 60, "min_drop": 20,
=======
        "enter_below": 130, "exit_above": 150, "good_below": 60, "min_drop": 20,
>>>>>>> 141742bef4e9f741f884417b472b6c97997cbeb6
        "shallow_msg": "curl higher",
    },
    "shoulder_press": {
        "measure": m_elbow_extension, "gate": g_press, "joints": ("shoulder", "elbow", "wrist"),
        "rest_state": "racked", "active_state": "pressing",
        "enter_below": 55, "exit_above": 75, "good_below": 25, "min_drop": 25,
        "shallow_msg": "press to full lockout",
    },
    "lateral_raise": {
        "measure": m_arm_raise, "gate": g_lateral, "joints": ("hip", "shoulder", "elbow"),
        "rest_state": "lowered", "active_state": "raising",
        "enter_below": 130, "exit_above": 150, "good_below": 100, "min_drop": 25,
        "shallow_msg": "raise to shoulder height",
    },
    "push_up": {
        "measure": m_elbow_angle, "gate": g_push_up, "joints": ("shoulder", "elbow", "wrist"),
        "rest_state": "up", "active_state": "down",
        "enter_below": 140, "exit_above": 160, "good_below": 95, "min_drop": 25,
        "shallow_msg": "go lower",
    },
}
# =============================================================================


# -----------------------------------------------------------------------------
# Websocket broadcaster (runs its own asyncio loop in a background thread so the
# camera loop can stay on the main thread, which OpenCV windows prefer on Windows)
# -----------------------------------------------------------------------------
class BroadcastServer:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.clients = set()
        self.loop = None
        self.error = None
        self.ready = threading.Event()
        self._stop = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    async def _handler(self, ws):
        self.clients.add(ws)
        try:
            async for _ in ws:      # ignore anything clients send; just keep connection open
                pass
        except Exception:
            pass
        finally:
            self.clients.discard(ws)

    async def _broadcast(self, message):
        clients = list(self.clients)
        if not clients:
            return
        results = await asyncio.gather(*(c.send(message) for c in clients), return_exceptions=True)
        for c, r in zip(clients, results):
            if isinstance(r, Exception):
                self.clients.discard(c)      # dead/disconnected client

    async def _main(self):
        self.loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        try:
            async with websockets.serve(self._handler, self.host, self.port):
                self.ready.set()
                await self._stop.wait()
        except OSError as e:                 # e.g. port already in use
            self.error = e
            self.ready.set()

    def _run(self):
        asyncio.run(self._main())

    def start(self):
        self._thread.start()
        self.ready.wait(timeout=10)
        if self.error:
            raise self.error

    def broadcast(self, payload: dict):
        """Thread-safe, non-blocking. Call from the camera loop."""
        if self.loop is None:
            return
        message = json.dumps(payload)
        asyncio.run_coroutine_threadsafe(self._broadcast(message), self.loop)

    def stop(self):
        if self.loop and self._stop:
            self.loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=2)


# -----------------------------------------------------------------------------
# Rep state machine (exercise-agnostic; driven by the EXERCISES catalog)
# -----------------------------------------------------------------------------
class Tally:
    """Rep totals. Shared across exercises (cumulative) or one per exercise."""

    def __init__(self):
        self.reps = 0
        self.good = 0

    @property
    def form_score(self):
        return round(100.0 * self.good / self.reps, 1) if self.reps else 0.0


class RepCounter:
    def __init__(self, name, cfg, tally):
        self.name = name
        self.cfg = cfg
        self.tally = tally
        self.state = cfg["rest_state"]
        self.min_value = None

    @property
    def is_active(self):
        return self.state != self.cfg["rest_state"]

    def start_active(self, min_value):
        """Used at detection time: the movement already started, so begin mid-rep."""
        self.state = self.cfg["active_state"]
        self.min_value = min_value

    def update(self, value):
        """Feed one signal value. Returns an event dict when a rep completes, else None."""
        c = self.cfg
        if self.state == c["rest_state"]:
            if value < c["enter_below"]:
                self.state = c["active_state"]
                self.min_value = value
        else:  # active state
            self.min_value = min(self.min_value, value)
            if value > c["exit_above"]:
                depth_ok = self.min_value < c["good_below"]
                self.tally.reps += 1
                if depth_ok:
                    self.tally.good += 1
                self.state = c["rest_state"]
                self.min_value = None
                return {
                    "event": "rep_counted",
                    "reps": self.tally.reps,
                    "depth_ok": depth_ok,
                    "form_score": self.tally.form_score,
                    "exercise": self.name,
                    "state": self.state,
                }
        return None


# -----------------------------------------------------------------------------
# Exercise detection
# Candidates are (exercise, side) pairs, e.g. ("squat", "left") = the left leg.
# -----------------------------------------------------------------------------
class ExerciseDetector:
    def __init__(self, allowed):
        self.keys = [(ex, side) for ex in allowed for side in SIDES]
        self.reset()

    def reset(self):
        self.armed = {k: False for k in self.keys}          # seen at rest recently?
        self.history = {k: deque() for k in self.keys}      # (time, value) over the recent window
        self.run_min = {k: float("inf") for k in self.keys}  # lowest value since last at rest
        self.lead_ex = None
        self.confirm = 0

    def update(self, signals, now, exclude_ex=None):
        """signals: {(ex, side): (value or None, gate_ok, hold_ok)}. Returns confirmed (ex, side) or None.
        exclude_ex: the exercise we are already locked to (still tracked, never a candidate)."""
        best_key, best_strength = None, 0.0
        for k in self.keys:
            value, gate, hold = signals.get(k, (None, False, False))
            hist = self.history[k]
            if value is None or not hold:                    # not visible / wrong posture: forget it
                hist.clear()
                self.armed[k] = False
                self.run_min[k] = float("inf")
                continue
            cfg = EXERCISES[k[0]]
            hist.append((now, value))
            while hist and now - hist[0][0] > DETECT_WINDOW_SECONDS:
                hist.popleft()
            if value > cfg["exit_above"]:                    # at rest -> armed, restart the min
                self.armed[k] = True
                self.run_min[k] = float("inf")
            else:
                self.run_min[k] = min(self.run_min[k], value)
            if k[0] != exclude_ex and self.armed[k] and gate and value < cfg["enter_below"]:
                strength = (max(x for _, x in hist) - value) / cfg["min_drop"]
                if strength >= 1.0 and strength > best_strength:   # compare across units
                    best_key, best_strength = k, strength

        if best_key is None:
            self.lead_ex, self.confirm = None, 0
            return None
        if best_key[0] == self.lead_ex:
            self.confirm += 1
        else:
            self.lead_ex, self.confirm = best_key[0], 1
        return best_key if self.confirm >= DETECT_CONFIRM_FRAMES else None


class Session:
    """Detect -> lock exercise -> count reps/check form -> (idle) -> detect again."""

    def __init__(self, allowed):
        self.allowed = list(allowed)
        self.detector = ExerciseDetector(self.allowed)
        self.keys = self.detector.keys
        shared = Tally()
        self.tallies = {ex: (Tally() if PER_EXERCISE_COUNTS else shared) for ex in EXERCISES}
        self.last_tally = shared
        self.counter = None
        self.key = None
        self.last_activity = 0.0
        self.active_since = 0.0

    @property
    def display_tally(self):
        return self.counter.tally if self.counter else self.last_tally

    def unlock(self, reason=None):
        if reason:
            print(reason)
        self.counter = None
        self.key = None
        self.detector.reset()

    def update(self, signals, now):
        """Returns a rep_counted event dict or None."""
        # The detector keeps watching even while locked: a DIFFERENT exercise that clearly
        # starts (armed + gate + drop, a few frames in a row) takes over immediately.
        locked_ex = self.counter.name if self.counter else None
        key = self.detector.update(signals, now, exclude_ex=locked_ex)
        if key is not None:
            ex, side = key
            self.key = key
            self.counter = RepCounter(ex, EXERCISES[ex], self.tallies[ex])
            self.counter.start_active(self.detector.run_min[key])
            self.last_tally = self.counter.tally
            self.last_activity = self.active_since = now
            self.detector.lead_ex, self.detector.confirm = None, 0
            print(f"{'switched to' if locked_ex else 'detected:'} {ex} ({side})")
            return None
        if self.counter is None:
            return None

        value = signals.get(self.key, (None,))[0]
        if value is not None:
            was_active = self.counter.is_active
            event = self.counter.update(value)
            if self.counter.is_active != was_active:
                self.last_activity = now
                if self.counter.is_active:
                    self.active_since = now
            if event is not None:
                self.last_activity = now
                return event

        if self.counter.is_active:
            if now - self.active_since > ACTIVE_TIMEOUT_SECONDS:
                self.unlock("stuck mid-rep too long, abandoning it and detecting again")
        elif now - self.last_activity > SWITCH_IDLE_SECONDS:
            self.unlock("idle, listening for a new exercise")
        return None


def compute_signals(P, keys):
    out = {}
    for ex, side in keys:
        cfg = EXERCISES[ex]
        try:
            v = cfg["measure"](P, side)
            g = bool(cfg["gate"](P, side)) if v is not None else False
            hd = bool(cfg.get("hold", cfg["gate"])(P, side)) if v is not None else False
        except (TypeError, ZeroDivisionError):
            v, g, hd = None, False, False
        out[(ex, side)] = (v, g, hd)
    return out


def smooth_signals(raw, prev):
    out = {}
    for k, (v, g, hd) in raw.items():
        if v is None:
            out[k] = (None, g, hd)
        else:
            p = prev.get(k, (None, False, False))[0]
            out[k] = (v if p is None else SIGNAL_SMOOTHING * v + (1 - SIGNAL_SMOOTHING) * p, g, hd)
    return out


# -----------------------------------------------------------------------------
# Overlay
# -----------------------------------------------------------------------------
def put_text(img, text, org, scale=0.7, color=(255, 255, 255), thick=2):
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), thick + 3, cv2.LINE_AA)
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, thick, cv2.LINE_AA)


def fmt(v):
    if v is None:
        return "--"
    return f"{v:.2f}" if abs(v) < 3 else f"{v:.0f}"


<<<<<<< HEAD
def draw_overlay(frame, session, signals, n_clients, last_result, lm=None):
    h, w = frame.shape[:2]
    # tell the person WHY nothing is detected when the pose itself is the problem
    hint = None
    if lm is None:
        hint = "No person found - step back so shoulders to feet are in view, light the room"
    elif session.counter is None and DEBUG_HINTS:
        vis = lambda i: lm[i].visibility >= MIN_VISIBILITY and 0 <= lm[i].y <= 1
        if not (vis(23) or vis(24)):
            hint = "Hips not in frame: arm exercises only (step back for squat/lunge/knee raise)"
        elif not (vis(27) or vis(28)):
            hint = "Feet not in frame: leg exercises need feet visible (step back)"
    if hint:
        put_text(frame, hint, (10, h - 32), 0.45, (0, 200, 255), 1)

    put_text(frame, "WS clients: " + str(n_clients) + "   R = re-detect   ESC = quit",
=======
def draw_overlay(frame, session, signals, n_clients, last_result):
    h, w = frame.shape[:2]
    counter = session.counter

    if counter is not None:
        ex, side = session.key
        put_text(frame, f"{ex.upper()} ({side})", (10, 28), 0.8, (0, 255, 255))
        put_text(frame, f"Signal: {fmt(signals.get(session.key, (None,))[0])}", (10, 58))
        put_text(frame, f"State: {counter.state}", (10, 86))
    else:
        put_text(frame, "DETECTING EXERCISE...", (10, 28), 0.8, (0, 165, 255))
        put_text(frame, "Start from the resting position, then move", (10, 58), 0.5, (0, 165, 255), 1)
        put_text(frame, "State: waiting", (10, 86))
        # live candidate panel: best side per exercise; A = armed, G = gate ok
        x0 = w - 250
        put_text(frame, "signal  A=armed G=gate", (x0, 20), 0.45, (200, 200, 200), 1)
        for i, ex in enumerate(session.allowed):
            best = None
            for side in SIDES:
                v, g, _ = signals.get((ex, side), (None, False, False))
                if v is not None and (best is None or v < best[0]):
                    best = (v, g, session.detector.armed[(ex, side)])
            if best is None:
                line = f"{ex:<15} --"
            else:
                line = f"{ex:<15}{fmt(best[0]):>4}  {'A' if best[2] else '.'}{'G' if best[1] else '.'}"
            put_text(frame, line, (x0, 42 + i * 20), 0.45, (255, 255, 255), 1)

    tally = session.display_tally
    put_text(frame, f"Reps: {tally.reps}", (10, 114), 0.8, (0, 255, 0))
    put_text(frame, f"Form: {tally.form_score:.1f}%", (10, 144), 0.8, (0, 255, 0))
    put_text(frame, f"WS clients: {n_clients}   R = re-detect   ESC = quit",
>>>>>>> 141742bef4e9f741f884417b472b6c97997cbeb6
             (10, h - 12), 0.45, (200, 200, 200), 1)

    if last_result is not None:
        ok, msg, ts = last_result
        if time.time() - ts < FEEDBACK_SECONDS:
<<<<<<< HEAD
            text, color = ("GOOD REP", (0, 255, 0)) if ok else ("SHALLOW - " + msg, (0, 0, 255))
=======
            text, color = ("GOOD REP", (0, 255, 0)) if ok else (f"SHALLOW - {msg}", (0, 0, 255))
>>>>>>> 141742bef4e9f741f884417b472b6c97997cbeb6
            put_text(frame, text, (w // 2 - 150, h - 60), 0.9, color, 3)


# -----------------------------------------------------------------------------
# Main loop
# -----------------------------------------------------------------------------
def resolve_allowed():
    if EXERCISE == "auto":
        return list(EXERCISES)
    names = [EXERCISE] if isinstance(EXERCISE, str) else list(EXERCISE)
    bad = [n for n in names if n not in EXERCISES]
    if bad or not names:
        sys.exit(f"Unknown EXERCISE {bad or EXERCISE}. Options: auto, {', '.join(EXERCISES)}")
    return names


def main():
    allowed = resolve_allowed()

    if not hasattr(mp, "solutions"):
        sys.exit("This mediapipe build has no mp.solutions. Use Python 3.12 and: "
                 "py -m pip install mediapipe==0.10.14")

    server = BroadcastServer(HOST, PORT)
    try:
        server.start()
    except OSError as e:
        sys.exit(f"Could not start websocket on {HOST}:{PORT} ({e}). Is replay.py or another main.py running?")
    print(f"websocket on {HOST}:{PORT}")
    print(f"listening for: {', '.join(allowed)}")

    cap = cv2.VideoCapture(CAMERA_INDEX)
    if not cap.isOpened():
        server.stop()
        sys.exit("Could not open webcam. Close other apps using it or change CAMERA_INDEX.")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)

    mp_pose = mp.solutions.pose
    mp_draw = mp.solutions.drawing_utils
    session = Session(allowed)

    smoothed = {}
    last_result = None      # (depth_ok, shallow message, timestamp) for the on-screen banner
    window = "Rehab Tracker (ESC to quit)"

    try:
        with mp_pose.Pose(
            model_complexity=MODEL_COMPLEXITY,
            min_detection_confidence=MIN_DETECTION_CONF,
            min_tracking_confidence=MIN_TRACKING_CONF,
        ) as pose:
            while True:
                ok, frame = cap.read()
                if not ok:
                    print("Camera read failed, stopping.")
                    break
                if MIRROR_VIEW:
                    frame = cv2.flip(frame, 1)
                h, w = frame.shape[:2]

                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                rgb.flags.writeable = False
                results = pose.process(rgb)

                lm = None
                if results.pose_landmarks:
                    mp_draw.draw_landmarks(frame, results.pose_landmarks, mp_pose.POSE_CONNECTIONS)
                    lm = results.pose_landmarks.landmark
                P = PoseFrame(lm, w, h)

                now = time.time()
                smoothed = smooth_signals(compute_signals(P, session.keys), smoothed)
                event = session.update(smoothed, now)
                if event is not None:
                    server.broadcast(event)                  # immediately
                    print(json.dumps(event))
                    msg = EXERCISES[event["exercise"]]["shallow_msg"]
                    last_result = (event["depth_ok"], msg, time.time())

                # Highlight the joints of the locked-in exercise
                if session.key is not None:
                    ex, side = session.key
                    for part in EXERCISES[ex]["joints"]:
                        p = P.pt(part, side)
                        if p is not None:
                            cv2.circle(frame, (int(p[0]), int(p[1])), 8, (0, 255, 255), -1)

<<<<<<< HEAD
                draw_overlay(frame, session, smoothed, len(server.clients), last_result, lm)
=======
                draw_overlay(frame, session, smoothed, len(server.clients), last_result)
>>>>>>> 141742bef4e9f741f884417b472b6c97997cbeb6
                cv2.imshow(window, frame)
                key = cv2.waitKey(1) & 0xFF
                if key == 27:                                # ESC
                    break
                if key in (ord("r"), ord("R")):
                    session.unlock("manual reset, detecting again")
                if cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:   # window X clicked
                    break
    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
        cv2.destroyAllWindows()
        server.stop()
        print("bye")


if __name__ == "__main__":
    main()
