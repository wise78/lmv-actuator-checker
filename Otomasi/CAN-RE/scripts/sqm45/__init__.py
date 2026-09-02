"""SQM45 bench-instrument engine: protocol, transport, and replay.

Layout:
    protocol.py   decode/encode a CAN payload. No serial, no threading.
    transport.py  turns (can_id, dlc, payload) into wire bytes and back.
                  TM7565Transport today; an ESP32 + MCP2515 adapter is
                  planned as a second implementation of the same interface.
    replay.py     the proven wake/hold/move transmit loop, built on a
                  transport rather than a raw serial port.

Nothing here imports a UI framework. sqm45_gui / a Flask app / a bare CLI
script all sit on top of this package without it knowing any of them exist.
"""
