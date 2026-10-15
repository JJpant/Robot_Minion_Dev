#------------------------------------------------------------------------------
#  gpio_compat.py  -  Couche de compatibilité "pigpio" --> "lgpio"
#
#  pigpio ne fonctionne pas sur Raspberry Pi 5 (puce RP1). Ce module reprend
#  la petite partie de l'API pigpio utilisée par le projet MINION, mais
#  s'appuie sur lgpio (fonctionne sur Pi 4 ET Pi 5, sans démon).
#
#  Utilisation :   import gpio_compat as pigpio
#                  pi = pigpio.pi()
#
#  ATTENTION : le PWM et les impulsions servo de lgpio sont temporisés par
#  logiciel (pigpio utilisait le DMA). Pour les servos, préférer les types
#  "pca9685" / "pca9685_onoff" (cf. data.json), qui n'ont pas de jitter.
#------------------------------------------------------------------------------

import lgpio

# Mêmes valeurs que pigpio
INPUT, OUTPUT = 0, 1
LOW, HIGH = 0, 1
PUD_OFF, PUD_DOWN, PUD_UP = 0, 1, 2

_PULL = {PUD_OFF: lgpio.SET_PULL_NONE,
         PUD_DOWN: lgpio.SET_PULL_DOWN,
         PUD_UP: lgpio.SET_PULL_UP}

_DUTY_RANGE = 255           # pigpio : set_PWM_dutycycle(gpio, 0..255)
_SERVO_HZ = 50              # pigpio : impulsions servo toujours à 50 Hz
_PWM_DEFAULT_HZ = 800       # fréquence par défaut de pigpio

_chip_number = None


def _find_chip():
    """Numéro du gpiochip qui porte les broches du connecteur 40 broches :
    Pi 5 --> puce RP1 ; Pi 4 --> bcm2711.  (le numéro varie selon le noyau)"""
    global _chip_number
    if _chip_number is not None:
        return _chip_number

    fallback = None
    for n in range(8):
        try:
            h = lgpio.gpiochip_open(n)
        except lgpio.error:
            continue
        try:
            info = lgpio.gpio_get_chip_info(h)
            label = str(info[3]).lower() if isinstance(info, (list, tuple)) and len(info) > 3 else ""
        finally:
            lgpio.gpiochip_close(h)
        if "rp1" in label:
            _chip_number = n
            return n
        if fallback is None and ("bcm2711" in label or "bcm2835" in label):
            fallback = n

    if fallback is None:
        raise RuntimeError("gpio_compat : aucun gpiochip Raspberry Pi trouvé")
    _chip_number = fallback
    return fallback


class pi(object):
    """Équivalent minimal de pigpio.pi()"""

    def __init__(self):
        self._h = lgpio.gpiochip_open(_find_chip())
        self.connected = True
        self._claimed = {}      # gpio -> OUTPUT / INPUT
        self._pull = {}         # gpio -> flag lgpio
        self._freq = {}         # gpio -> fréquence PWM (Hz)
        self._last = {}         # gpio -> dernier réglage PWM/servo envoyé

    # ---------------------------------------------------------------- interne
    def _release(self, gpio):
        if gpio in self._claimed:
            lgpio.gpio_free(self._h, gpio)
            del self._claimed[gpio]
        self._last.pop(gpio, None)

    def _claim_output(self, gpio):
        if self._claimed.get(gpio) != OUTPUT:
            self._release(gpio)
            lgpio.gpio_claim_output(self._h, gpio, 0, 0)
            self._claimed[gpio] = OUTPUT

    def _claim_input(self, gpio):
        self._release(gpio)
        lgpio.gpio_claim_input(self._h, gpio, self._pull.get(gpio, lgpio.SET_PULL_NONE))
        self._claimed[gpio] = INPUT

    # ------------------------------------------------------------- API pigpio
    def set_mode(self, gpio, mode):
        if mode == OUTPUT:
            self._claim_output(gpio)
        else:
            self._claim_input(gpio)
        return 0

    def set_pull_up_down(self, gpio, pud):
        self._pull[gpio] = _PULL[pud]
        if self._claimed.get(gpio) == INPUT:        # lgpio : pull appliquée au claim
            self._claim_input(gpio)
        return 0

    def write(self, gpio, level):
        self._claim_output(gpio)
        lgpio.gpio_write(self._h, gpio, 1 if level else 0)
        return 0

    def read(self, gpio):
        if gpio not in self._claimed:
            self._claim_input(gpio)
        return lgpio.gpio_read(self._h, gpio)

    def set_PWM_frequency(self, gpio, frequency):
        self._freq[gpio] = frequency
        return frequency

    def set_PWM_dutycycle(self, gpio, dutycycle):
        """dutycycle : 0..255 (comme pigpio)"""
        dutycycle = max(0, min(_DUTY_RANGE, int(dutycycle)))
        freq = self._freq.get(gpio, _PWM_DEFAULT_HZ)
        if self._last.get(gpio) == ("pwm", freq, dutycycle):
            return 0                                # rien à changer
        self._claim_output(gpio)
        lgpio.tx_pwm(self._h, gpio, freq, dutycycle * 100.0 / _DUTY_RANGE)
        self._last[gpio] = ("pwm", freq, dutycycle)
        return 0

    def set_servo_pulsewidth(self, gpio, pulsewidth):
        """pulsewidth : 0 (arrêt) ou 500..2500 µs (comme pigpio)"""
        pulsewidth = int(pulsewidth)
        if pulsewidth != 0:
            pulsewidth = max(500, min(2500, pulsewidth))
        if self._last.get(gpio) == ("servo", pulsewidth):
            return 0
        self._claim_output(gpio)
        lgpio.tx_servo(self._h, gpio, pulsewidth, _SERVO_HZ)
        self._last[gpio] = ("servo", pulsewidth)
        return 0

    def stop(self):
        if self._h is None:
            return
        for gpio, mode in list(self._claimed.items()):
            try:
                if mode == OUTPUT:
                    lgpio.tx_pwm(self._h, gpio, 0, 0)   # coupe un éventuel PWM
                lgpio.gpio_free(self._h, gpio)
            except lgpio.error:
                pass
        self._claimed.clear()
        try:
            lgpio.gpiochip_close(self._h)
        except lgpio.error:
            pass
        self._h = None
        self.connected = False
