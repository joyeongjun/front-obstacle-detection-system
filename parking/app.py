
from flask import Flask, render_template, jsonify, request
from gpiozero import AngularServo, DistanceSensor, LED, TonalBuzzer
from threading import Thread, Lock, Event
from time import sleep, monotonic
import atexit

app = Flask(__name__)

# ==================================
# GPIO 설정
# ==================================

# 서보모터 GPIO12
servo = AngularServo(
    12,
    min_angle=-90,
    max_angle=90,
    min_pulse_width=0.001,
    max_pulse_width=0.002
)

# 초음파 센서
sensor = DistanceSensor(
    echo=24,
    trigger=23,
    max_distance=2,
    queue_len=5
)

# LED
red_led = LED(27)
yellow_led = LED(22)

# 수동형 부저 GPIO18
buzzer = TonalBuzzer(17)

# ==================================
# 서보모터 설정
# ==================================

# 왼쪽 -90도 / 중앙 0도 / 오른쪽 90도
POSITIONS = [
    ("left", -90),
    ("center", 0),
    ("right", 90),
    ("center", 0)
]

# 5도씩 천천히 이동
SERVO_STEP = 10
SERVO_DELAY = 0.00002

current_angle = 0

lock = Lock()
stop_event = Event()

# ==================================
# 시스템 상태
# ==================================

state = {
    "left": None,
    "center": None,
    "right": None,
    "direction": "center",
    "distance": None,
    "status": "WAIT",
    "message": "시스템 시작 중",
    "buzzer_enabled": True,
    "scanning": True
}

# ==================================
# 거리별 위험 상태
# ==================================

def classify(distance):

    if distance is None:
        return "ERROR", "거리 측정 불가"

    if distance <= 5:
        return "STOP", "즉시 정지!"

    if distance <= 15:
        return "DANGER", "충돌 위험"

    if distance <= 30:
        return "CAUTION", "장애물 주의"

    return "SAFE", "안전"


# ==================================
# 초음파 거리 측정
# ==================================

def measure_distance():

    try:
        value = sensor.distance * 100

        if value >= 199:
            return None

        return round(value, 1)

    except Exception as e:
        print("센서 오류:", e)
        return None


# ==================================
# LED 및 부저 제어
# ==================================

def update_outputs(status, enabled, now):

    # LED 제어
    if status in ("STOP", "DANGER"):
        red_led.on()
        yellow_led.off()

    elif status == "CAUTION":
        red_led.off()
        yellow_led.on()

    else:
        red_led.off()
        yellow_led.off()

    # 부저 제어
    if not enabled:
        buzzer.stop()

    elif status == "STOP":
        # 5cm 이하: 연속 경고음
        buzzer.play(880)

    elif status == "DANGER":
        # 5~15cm: 빠른 경고음
        if now % 0.2 < 0.1:
            buzzer.play(660)
        else:
            buzzer.stop()

    elif status == "CAUTION":
        # 15~30cm: 느린 경고음
        if now % 0.6 < 0.1:
            buzzer.play(440)
        else:
            buzzer.stop()

    else:
        buzzer.stop()


# ==================================
# 경고 장치 유지하면서 대기
# ==================================

def wait_with_alarm(seconds):

    end_time = monotonic() + seconds

    while monotonic() < end_time:

        if stop_event.is_set():
            return

        with lock:
            distances = [
                state["left"],
                state["center"],
                state["right"]
            ]

            enabled = state["buzzer_enabled"]

        valid = [
            d for d in distances
            if d is not None
        ]

        if valid:
            nearest = min(valid)
            status, message = classify(nearest)

        else:
            nearest = None
            status, message = classify(None)

        with lock:
            state["distance"] = nearest
            state["status"] = status
            state["message"] = message

        update_outputs(
            status,
            enabled,
            monotonic()
        )

        sleep(0.02)


# ==================================
# 서보모터 부드럽게 이동
# ==================================

def smooth_move(target_angle):

    global current_angle

    start_angle = current_angle

    if target_angle > start_angle:
        step = SERVO_STEP

    elif target_angle < start_angle:
        step = -SERVO_STEP

    else:
        return

    for angle in range(
        start_angle,
        target_angle,
        step
    ):

        if stop_event.is_set():
            return

        servo.angle = angle
        wait_with_alarm(SERVO_DELAY)

    servo.angle = target_angle

    # 위치 안정화
    wait_with_alarm(0.2)

    # 정지 후 떨림 완화
    servo.detach()

    current_angle = target_angle


# ==================================
# 자동 스캔
# ==================================

def scan_loop():

    try:
        # 시작 위치 중앙
        servo.angle = 0
        wait_with_alarm(0.5)
        servo.detach()

        while not stop_event.is_set():

            for direction, angle in POSITIONS:

                if stop_event.is_set():
                    break

                with lock:
                    state["direction"] = direction

                print(
                    f"스캔 방향: {direction}, "
                    f"목표 각도: {angle}"
                )

                smooth_move(angle)

                if stop_event.is_set():
                    break

                # 초음파 거리 측정
                distance = measure_distance()

                with lock:
                    state[direction] = distance

                print(
                    f"{direction}: {distance} cm"
                )

                wait_with_alarm(0.15)

    except Exception as e:

        print("스캔 오류:", e)

        with lock:
            state["status"] = "ERROR"
            state["message"] = "스캔 장치 오류"

    finally:
        red_led.off()
        yellow_led.off()
        buzzer.stop()
        servo.detach()


# ==================================
# Flask 메인 페이지
# ==================================

@app.route("/")
def home():
    return render_template("index.html")


# ==================================
# 실시간 상태 API
# ==================================

@app.route("/api/status")
def api_status():

    with lock:
        return jsonify(state.copy())


# ==================================
# 부저 ON / OFF API
# ==================================

@app.route("/api/buzzer", methods=["POST"])
def api_buzzer():

    data = request.get_json(silent=True) or {}

    enabled = data.get("enabled")

    if not isinstance(enabled, bool):
        return jsonify({
            "error": "잘못된 값"
        }), 400

    with lock:
        state["buzzer_enabled"] = enabled

    if not enabled:
        buzzer.stop()

    return jsonify({
        "ok": True
    })


# ==================================
# GPIO 정리
# ==================================

def cleanup():

    stop_event.set()

    buzzer.stop()
    red_led.off()
    yellow_led.off()

    servo.detach()

    buzzer.close()
    red_led.close()
    yellow_led.close()

    sensor.close()
    servo.close()


atexit.register(cleanup)


# ==================================
# 프로그램 시작
# ==================================

if __name__ == "__main__":

    Thread(
        target=scan_loop,
        daemon=True
    ).start()

    app.run(
        host="0.0.0.0",
        port=8080,
        debug=False,
        use_reloader=False
    )
