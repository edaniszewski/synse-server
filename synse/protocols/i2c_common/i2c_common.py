#!/usr/bin/env python
""" Common I2C operations.

    \\//
     \/apor IO

-------------------------------
Copyright (C) 2015-17  Vapor IO

This file is part of Synse.

Synse is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 2 of the License, or
(at your option) any later version.

Synse is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with Synse.  If not, see <http://www.gnu.org/licenses/>.
"""
# pylint: disable=import-error,line-too-long

import copy
import datetime
import logging
import struct
import time
from binascii import hexlify

from mpsse import (ACK, GPIOL0, GPIOL1, I2C, IFACE_A, IFACE_B, MPSSE, MSB,
                   ONE_HUNDRED_KHZ)

from synse.protocols.conversions import conversions
from synse.stats import stats

logger = logging.getLogger(__name__)

# Proto 2 Board
# PCA9546A is a four channel I2C / SMBus switch.
PCA9546_WRITE_ADDRESS = '\xE2'
PCA9546_READ_ADDRESS = '\xE3'

# The number of times we read a differential pressure sensor in order to handle
# the turbulence that causes single reads to vary wildly.
DIFFERENTIAL_PRESSURE_READ_COUNT = 25


# Masks for lock controls by lock number.
LOCK_CONTROL1 = 0x08
LOCK_CONTROL2 = 0x01
LOCK_CONTROL3 = 0x04
LOCK_CONTROL4 = 0x20
LOCK_CONTROL5 = 0x01
LOCK_CONTROL6 = 0x02
LOCK_CONTROL7 = 0x04
LOCK_CONTROL8 = 0x08
LOCK_CONTROL9 = 0x10
LOCK_CONTROL10 = 0x20
LOCK_CONTROL11 = 0x40
LOCK_CONTROL12 = 0x80

# Doing a quick lock control of the rev 1 hardware which only has one lock
# here are the bit locations associated with the lock signals
# Control line is on GPA7 (Port A bit 7) of the MCP23017
# ELS is on GPA6 (Port A bit 6) of the MCP23017
# MLS is on GPA5 (Port A bit 5) of the MCP23017
# Using this value for masking for the rev 1 board.
CONTROL = 0x80

# Electronic Lock Status by lock number.
ELS1 = 0x10
ELS2 = 0x02
ELS3 = 0x02
ELS4 = 0x10
ELS5 = 0x01
ELS6 = 0x02
ELS7 = 0x04
ELS8 = 0x08
ELS9 = 0x10
ELS10 = 0x20
ELS11 = 0x40
ELS12 = 0x80

# Mechanical Lock Status by lock number.
MLS1 = 0x20
MLS2 = 0x04
MLS3 = 0x01
MLS4 = 0x08
MLS5 = 0x01
MLS6 = 0x02
MLS7 = 0x04
MLS8 = 0x08
MLS9 = 0x10
MLS10 = 0x20
MLS11 = 0x40
MLS12 = 0x80

# GPIO expander addresses and registers
# Lock rev 1 hardware settings
WRITE_23017 = '\x40'
READ_23017 = '\x41'

# Lock rev 2 hardware.
WRITE_23008 = '\x40'
READ_23008 = '\x41'
IODIRA_23017 = '\x00'
IODIRB_23017 = '\x01'
GPIOA_23017 = '\x12'
GPIOB_23017 = '\x13'
OLATA_23017 = '\x14'
OLATB_23017 = '\x15'
IODIR_23008 = '\x00'
GPIO_23008 = '\x09'
OLAT_23008 = '\x0A'

# I2C switch
WRITE_9546A = '\xE2'
READ_9546A = '\xE3'


def _get_lock_parameters(vec_a, vec_b, lock_number):
    """
    Gets the lock parameters to send out given the devices and the lock index.
    :param vec_a: Open handle to FT4232H IFACE_A.
    :param vec_b: Open handle to FT4232H IFACE_B.
    :param lock_number: Index of the lock to operate on (1-12).
    :return: mask, latch, vec
    :raises ValueError: Invalid parameter.
    """
    if vec_a is None:
        raise ValueError('vec_a is None')
    if vec_b is None:
        raise ValueError('vec_b is None')

    # Assign control bit position according to lock number and GPIO addresses
    # Since the associated latch bit needs set, OR with latch to set.
    if lock_number == 1:
        # rev 1
        mask = 0x80
        latch = IODIRA_23017
        vec = vec_b

        # rev 2
        # mask = CONTROL1
        # latch = OLATB_23017
        # vec = device_b

    # Rev1 only has one lock, so the rest are for rev2.
    elif lock_number == 2:
        mask = LOCK_CONTROL2
        latch = OLATB_23017
        vec = vec_b

    elif lock_number == 3:
        mask = LOCK_CONTROL3
        latch = OLATA_23017
        vec = vec_b

    elif lock_number == 4:
        mask = LOCK_CONTROL4
        latch = OLATA_23017
        vec = vec_b

    elif lock_number == 5:
        mask = LOCK_CONTROL5
        latch = OLATA_23017
        vec = vec_a

    elif lock_number == 6:
        mask = LOCK_CONTROL6
        latch = OLATA_23017
        vec = vec_a

    elif lock_number == 7:
        mask = LOCK_CONTROL7
        latch = OLATA_23017
        vec = vec_a

    elif lock_number == 8:
        mask = LOCK_CONTROL8
        latch = OLATA_23017
        vec = vec_a

    elif lock_number == 9:
        mask = LOCK_CONTROL9
        latch = OLATA_23017
        vec = vec_a

    elif lock_number == 10:
        mask = LOCK_CONTROL10
        latch = OLATA_23017
        vec = vec_a

    elif lock_number == 11:
        mask = LOCK_CONTROL11
        latch = OLATA_23017
        vec = vec_a

    elif lock_number == 12:
        mask = LOCK_CONTROL12
        latch = OLATA_23017
        vec = vec_a

    else:
        raise ValueError('Invalid lock index {}. 1-12 are supported.'.format(lock_number))

    logger.debug('mask: {:02x}, latch: {}, vec: {}'.format(mask, hexlify(latch), vec))
    return mask, latch, vec


def _read_latch(vec, latch):
    """
    Reads the latch register so as not to disturb and prior lock settings.
    :param vec: The open handle to the FT4232H interface.
    :param latch: the latch to read.
    :return: The latch reading as an int.
    """
    # First read out the associated latch register so as not to
    # disturb any prior lock settings
    logger.debug('reading latch: 0x{}'.format(hexlify(latch)))
    to_send = WRITE_23017
    to_send += latch
    logger.debug('to_send: {}'.format(hexlify(to_send)))

    vec.Start()
    vec.Write(to_send)
    if vec.GetAck() == ACK:
        logger.debug('Got Ack')
    else:
        logger.debug('No Ack')
    vec.Stop()

    vec.Start()
    to_send = READ_23017
    logger.debug('to_send: {}'.format(hexlify(to_send)))
    vec.Write(to_send)
    if vec.GetAck() == ACK:
        logger.debug('Got Ack')
    else:
        logger.debug('No Ack')
    vec.SendNacks()
    reading = vec.Read(1)
    vec.Stop()
    vec.SendAcks()

    logger.debug('Read latch 0x{}: 0x{}'.format(hexlify(latch), hexlify(reading)))

    # Need int for bitwise operations.
    reading = struct.unpack('>B', reading)[0]
    return reading


def _set_channel3(vec):
    """
    Set the PCA9546A switch to channel three.
    :param vec: The port (interface) on the switch.
    """
    logger.debug('Setting switch to channel 3.')
    to_send = WRITE_9546A
    to_send += '\x08'  # Channel 3

    logger.debug('to_send: {}'.format(hexlify(to_send)))

    vec.Start()
    vec.Write(to_send)
    if vec.GetAck() == ACK:
        logger.debug('Got Ack')
    else:
        logger.debug('No Ack')
    vec.Stop()
    logger.debug('Set switch to channel 3.')


def _write_latch(vec, latch, data):
    """
    Write data to a latch on a device.
    :param vec: The open handle to the FT4232H interface.
    :param latch: the latch to read.
    :param data: The data to write.
    :return:
    """
    logger.debug('_write_latch start. latch 0x{}. data 0x{}'.format(
        hexlify(latch), format(hexlify(data))))
    to_send = WRITE_23017
    to_send += latch
    to_send += data
    logger.debug('to_send: {}'.format(hexlify(to_send)))

    vec.Start()
    vec.Write(to_send)
    vec.Stop()
    logger.debug('_write_latch end')


def _open_vec_i2c():
    """Open the i2c port on the VEC USB for i2c operations."""
    # Port A I2C for PCA9546A
    vec = MPSSE()
    vec.Open(0x0403, 0x6011, I2C, ONE_HUNDRED_KHZ, MSB, IFACE_A)

    # Port B I2C for debug leds (don't need the io expander for the DPS sensors)
    gpio = MPSSE()
    gpio.Open(0x0403, 0x6011, I2C, ONE_HUNDRED_KHZ, MSB, IFACE_B)

    # Set RESET line on PCA9546A to high to activate switch
    vec.PinHigh(GPIOL0)
    time.sleep(0.001)

    # Read channel of PCA9546A
    vec.Start()
    vec.Write(PCA9546_READ_ADDRESS)

    return vec, gpio


def lock_lock(lock_number):
    """
    Lock a lock by index (1-12)
    :param lock_number: The index of the lock to lock.
    """
    logger.debug('Locking lock {}'.format(lock_number))
    vec_a = _open_i2c_lock(IFACE_A)
    vec_b = _open_i2c_lock(IFACE_B)

    # Make sure reset line is held high on MCP23017 in order to use it
    vec_a.PinHigh(GPIOL1)
    time.sleep(0.005)

    # Get the mask, latch, and vec to operate on.
    mask, latch, vec = _get_lock_parameters(vec_a, vec_b, lock_number)

    # All the addresses and handles are assigned above based on the lock number.
    # Locks 1-4 are on the backplane and 5-12 are on the expansion which is
    # shared on channel 3 of the PCA9546A the buffered I2C shared with the led
    # controller and thermistors. At this point the only thing to do different
    # between the BP and expansion is set the channel on the PCA9546A to access
    # locks 5 - 12.
    if lock_number >= 5:
        _set_channel3(vec_a)

    # First read out the associated latch register so as not to
    # disturb any prior lock settings
    reading = _read_latch(vec, latch)

    # Lock
    _write_latch(vec, latch, chr(reading | mask))

    # ALL THE REMAINING LINES IN THIS FUNCTION ARE FOR DEBUG PURPOSES AND CAN BE REMOVED,
    # READ OUT THE IO DIRECTION REGISTER TO VERIFY CONTENTS.
    # We should get 0xFF. Leaving this code in until we get the rev 2 board.
    logger.debug('reading register')
    vec.Start()
    vec.Write(WRITE_23017 + IODIRA_23017)
    vec.Stop()
    vec.Start()
    vec.Write(READ_23017)
    vec.SendNacks()
    direction = vec.Read(1)
    vec.Stop()
    vec.SendAcks()
    print 'MCP23017 IO Direction Port A Register: 0x%0.2X' % ord(direction)

    _close_i2c(vec_a)
    _close_i2c(vec_b)


def lock_momentary_unlock(lock_number):
    """
    Unlock a lock by index (1-12)
    :param lock_number: The index of the lock to lock.
    """
    logger.debug('Unlocking lock {}'.format(lock_number))
    vec_a = _open_i2c_lock(IFACE_A)
    vec_b = _open_i2c_lock(IFACE_B)

    # Make sure reset line is held high on MCP23017 in order to use it
    vec_a.PinHigh(GPIOL1)
    time.sleep(0.005)

    # Get the mask, latch, and vec to operate on.
    mask, latch, vec = _get_lock_parameters(vec_a, vec_b, lock_number)

    # All the addresses and handles are assigned above based on the lock number.
    # Locks 1-4 are on the backplane and 5-12 are on the expansion which is
    # shared on channel 3 of the PCA9546A the buffered I2C shared with the led
    # controller and thermistors. At this point the only thing to do different
    # between the BP and expansion is set the channel on the PCA9546A to access
    # locks 5 - 12.
    if lock_number >= 5:
        _set_channel3(vec_a)

    # First read out the associated latch register so as not to
    # disturb any prior lock settings
    reading = _read_latch(vec, latch)

    # Unlock
    _write_latch(vec, latch, chr(reading & ~mask))

    # After trial an error setting to 70ms in python works, 50ms must be too fast.
    time.sleep(0.070)

    # now open the control line again by clearing the control bit assigned in mask
    _write_latch(vec, latch, chr(reading | mask))
    _close_i2c(vec_a)
    _close_i2c(vec_b)


def _open_i2c_lock(interface):
    """
    Open and initialize the port in the FT4232H for i2c lock operations.
    :param interface: The port index of the FT4232H. IFACE_A or IFACE_B.
    :return: Handle to i2c interface for the port.
    """
    vec = MPSSE()
    vec.Open(
        0x0403,  # VID
        0x6011,  # PID
        I2C,     # mode
        ONE_HUNDRED_KHZ,  # Clock frequency
        MSB,  # endianess. I2C is always MSB.
        interface)  # interface
    return vec


# This is for the rev 2 board which we don't have yet. (12/19/17)
def _check_els(vec_a, vec_b):
    """
    Checks the electronic lock status. Bit 0 is lock 1, etc.
    :param vec_a: Handle to i2c port A.
    :param vec_b: Handle to i2c port B.
    :return: bitmap where bit 0 is MLS1, bit 1 is MLS2, etc.
    """
    # read out the GPIO B register
    logger.debug('Reading GPIO B register')
    to_send = WRITE_23017
    to_send += GPIOB_23017
    logger.debug('to_send: {}'.format(hexlify(to_send)))

    vec_b.Start()
    vec_b.Write(to_send)
    if vec_b.GetAck() == ACK:
        logger.debug('Got Ack')
    else:
        logger.debug('No Ack')
    vec_b.Stop()

    to_send = READ_23017
    logger.debug('to_send: {}'.format(hexlify(to_send)))

    vec_b.Start()
    vec_b.Write(to_send)
    if vec_b.GetAck() == ACK:
        logger.debug('Got Ack')
    else:
        logger.debug('No Ack')
    reading = vec_b.Read(1)
    vec_b.SendNacks()
    vec_b.Stop()

    logger.debug('Read GPIO B register: {}'.format(hexlify(reading)))

    # Need int for bitwise operations.
    reading = struct.unpack('>B', reading)[0]

    # shift down ELS1 to bit 0
    bit = (reading & ELS1) >> 4

    # store in ELS
    els = bit

    # ELS2 is already at bit location 1
    bit = reading & ELS2

    # store in ELS
    els |= bit

    # read out the GPIO A register
    logger.debug('Reading GPIO A register')
    to_send = WRITE_23017
    to_send += GPIOA_23017

    logger.debug('to_send: {}'.format(hexlify(to_send)))
    vec_b.Start()
    vec_b.Write(to_send)
    if vec_b.GetAck() == ACK:
        logger.debug('Got Ack')
    else:
        logger.debug('No Ack')
    vec_b.Stop()

    to_send = READ_23017
    vec_b.Start()
    vec_b.Write(to_send)
    if vec_b.GetAck() == ACK:
        logger.debug('Got Ack')
    else:
        logger.debug('No Ack')
    reading = vec_b.Read(1)
    vec_b.SendNacks()
    vec_b.Stop()

    logger.debug('Read GPIO B register: {}'.format(hexlify(reading)))

    # Need int for bitwise operations.
    reading = struct.unpack('>B', reading)[0]

    # shift up ELS3 to bit 3
    bit = (reading & ELS3) << 1

    # store in ELS
    els |= bit

    # shift down ELS4 to bit location 4
    els |= (reading & MLS4) >> 1

    # now read out MLS 5 -12 from the expansion board
    # need to set the switch to channel 3 first
    _set_channel3(vec_a)

    # All the ELS lines are connected to the MCP23017 B port
    # read out the GPIO register
    logger.debug('Reading GPIO register')
    to_send = WRITE_23017
    to_send += GPIOB_23017

    vec_a.Start()
    vec_a.Write(to_send)
    if vec_b.GetAck() == ACK:
        logger.debug('Got Ack')
    else:
        logger.debug('No Ack')
    vec_a.Stop()

    to_send = READ_23017
    logger.debug('to_send: {}'.format(hexlify(to_send)))

    vec_a.Start()
    vec_a.Write(to_send)
    if vec_b.GetAck() == ACK:
        logger.debug('Got Ack')
    else:
        logger.debug('No Ack')
    reading = vec_a.Read(1)
    vec_a.SendNacks()
    vec_a.Stop()

    logger.debug('Read GPIO register: {}'.format(hexlify(reading)))

    # Need int for bitwise operations.
    reading = struct.unpack('>B', reading)[0]

    # Shift the entire byte up by 4 so to pack it
    # with locks 1 - 4
    bit = (reading << 4)
    els |= bit
    return els


# This is for the rev 2 board which we don't have yet. (12/19/17)
def _check_mls(vec_a, vec_b):
    """
    Checks the mechanical lock status. Bit 0 is lock 1, etc.
    :param vec_a: Handle to i2c port A.
    :param vec_b: Handle to i2c port B.
    :return: bitmap where bit 0 is MLS1, bit 1 is MLS2, etc.
    """
    # read out the GPIO B register
    logger.debug('Reading GPIO B register')
    to_send = WRITE_23017
    to_send += GPIOB_23017
    logger.debug('to_send: {}'.format(hexlify(to_send)))

    vec_b.Start()
    vec_b.Write(to_send)
    if vec_b.GetAck() == ACK:
        logger.debug('Got Ack')
    else:
        logger.debug('No Ack')
    vec_b.Stop()

    to_send = READ_23017
    logger.debug('to_send: {}'.format(hexlify(to_send)))

    vec_b.Start()
    vec_b.Write(to_send)
    if vec_b.GetAck() == ACK:
        logger.debug('Got Ack')
    else:
        logger.debug('No Ack')
    reading = vec_b.Read(1)
    vec_b.SendNacks()
    vec_b.Stop()

    logger.debug('Read GPIO B register: {}'.format(hexlify(reading)))

    # Need int for bitwise operations.
    reading = struct.unpack('>B', reading)[0]

    # shift down MLS1 to bit 0
    bit = (reading & MLS1) >> 5

    # store in MLS
    mls = bit

    # shift down MLS2 to bit 1
    bit = (reading & MLS2) >> 1

    # store in MLS
    mls |= bit

    # read out the GPIO A register
    logger.debug('Reading GPIO A register')
    to_send = WRITE_23017
    to_send += GPIOA_23017

    logger.debug('to_send: {}'.format(hexlify(to_send)))
    vec_b.Start()
    vec_b.Write(to_send)
    if vec_b.GetAck() == ACK:
        logger.debug('Got Ack')
    else:
        logger.debug('No Ack')
    vec_b.Stop()

    to_send = READ_23017
    vec_b.Start()
    vec_b.Write(to_send)
    if vec_b.GetAck() == ACK:
        logger.debug('Got Ack')
    else:
        logger.debug('No Ack')
    reading = vec_b.Read(1)
    vec_b.SendNacks()
    vec_b.Stop()

    logger.debug('Read GPIO B register: {}'.format(hexlify(reading)))

    # Need int for bitwise operations.
    reading = struct.unpack('>B', reading)[0]

    # shift up MLS3 to bit 2
    bit = (reading & MLS3) << 2

    # store in MLS
    mls |= bit

    # MLS4 is already in bit 3 so no need to shift
    mls |= (reading & MLS4)

    # now read out MLS 5 -12 from the expansion board
    # need to set the switch to channel 3 first
    _set_channel3(vec_a)
    # logger.debug('Setting switch to channel 3.')
    # to_send = WRITE_9546A
    # to_send += '\x08'  # Channel 3
    #
    # logger.debug('to_send: {}'.format(hexlify(to_send)))
    #
    # vec_a.Start()
    # vec_a.Write(to_send)
    # if vec_b.GetAck() == ACK:
    #     logger.debug('Got Ack')
    # else:
    #     logger.debug('No Ack')
    # vec_a.Stop()
    # logger.debug('Set switch to channel 3.')
    # TODO: end function to set the switch.

    # All the MLS lines are connected to the MCP23008
    # read out the GPIO register
    logger.debug('Reading GPIO register')
    to_send = WRITE_23008
    to_send += GPIO_23008

    vec_a.Start()
    vec_a.Write(to_send)
    if vec_b.GetAck() == ACK:
        logger.debug('Got Ack')
    else:
        logger.debug('No Ack')
    vec_a.Stop()

    to_send = READ_23017
    logger.debug('to_send: {}'.format(hexlify(to_send)))

    vec_a.Start()
    vec_a.Write(to_send)
    if vec_b.GetAck() == ACK:
        logger.debug('Got Ack')
    else:
        logger.debug('No Ack')
    reading = vec_a.Read(1)
    vec_a.SendNacks()
    vec_a.Stop()

    logger.debug('Read GPIO register: {}'.format(hexlify(reading)))

    # Need int for bitwise operations.
    reading = struct.unpack('>B', reading)[0]

    # Shift the entire byte up by 4 so to pack it
    # with locks 1 - 4
    bit = (reading << 4)
    mls |= bit
    return mls


def lock_status(lock_number):
    """
    Get the status of a lock.
    :param lock_number: Lock 1-12
    :return:
    0 = Alarms inactive Lock secure
    1 = ELS active lock electrically released
    2 = MLS active lock mechanical released
    3 = ELS and MLS active, handle not fully closed
    0 = ELS and MLS Active - Door Handle Not Secured.
    1 = ELS Active - Door Electrically Unlocked.
    2 = MLS Active - Door Unlocked by Key.
    3 = Door Lock Secure.
    """
    logger.debug('Lock Status')

    if not 1 <= lock_number <= 12:
        raise ValueError('lock_number {} must be between 1 and 12.')

    vec_a = _open_i2c_lock(IFACE_A)
    vec_b = _open_i2c_lock(IFACE_B)

    # Make sure reset line is held high on MCP23017 in order to use it
    vec_a.PinHigh(GPIOL1)
    time.sleep(0.005)

    # Rev1 board:
    vec_b.Start()
    vec_b.Write(WRITE_23017 + GPIOA_23017)
    vec_b.Stop()
    vec_b.Start()
    vec_b.Write(READ_23017)
    vec_b.SendNacks()

    status = vec_b.Read(1)
    vec_b.Stop()
    vec_b.SendAcks()

    status = ord(status)
    logger.debug('read status: 0x{:02x}'.format(status))
    status = (status >> 5) & 0x03
    logger.debug('shifted status: 0x{:02x}'.format(status))

    # Below is for the rev2 board which we don't have yet.
    # els = _check_els(vec_a, vec_b)
    # logger.debug('lock_status els: 0x{:02x}'.format(els))
    #
    # mls = _check_mls(vec_a, vec_b)
    # logger.debug('lock_status mls: 0x{:02x}'.format(mls))
    #
    # # combine MLS and ELS into a 2 bit number
    # # Bit 0 = ELS
    # # Bit 1 = MLS
    # # ELS can be shifted down by lock number - 1
    # els >>= (lock_number - 1)
    #
    # # MLS is a little more tricky, lock one has to shifted up by 1
    # # Lock 2 gets no shift
    # # Lock > 2 gets shifted down by lock number - 2
    # if lock_number == 1:
    #     mls <<= 1
    # elif lock_number > 2:
    #     els >>= (lock_number - 2)
    #
    # # mask out the other bit and combine into status pointer
    # els &= 0x0001
    # mls &= 0x0002
    # status = els & mls

    _close_i2c(vec_a)
    _close_i2c(vec_b)
    return status


def lock_unlock(lock_number):
    """
    Unlock a lock by index (1-12)
    :param lock_number: The index of the lock to lock.
    """
    logger.debug('Unlocking lock {}'.format(lock_number))
    vec_a = _open_i2c_lock(IFACE_A)
    vec_b = _open_i2c_lock(IFACE_B)

    # Make sure reset line is held high on MCP23017 in order to use it
    vec_a.PinHigh(GPIOL1)
    time.sleep(0.005)

    # Get the mask, latch, and vec to operate on.
    mask, latch, vec = _get_lock_parameters(vec_a, vec_b, lock_number)

    # All the addresses and handles are assigned above based on the lock number.
    # Locks 1-4 are on the backplane and 5-12 are on the expansion which is
    # shared on channel 3 of the PCA9546A the buffered I2C shared with the led
    # controller and thermistors. At this point the only thing to do different
    # between the BP and expansion is set the channel on the PCA9546A to access
    # locks 5 - 12.
    if lock_number >= 5:
        _set_channel3(vec_a)

    # First read out the associated latch register so as not to
    # disturb any prior lock settings
    reading = _read_latch(vec, latch)

    # Unlock
    _write_latch(vec, latch, chr(reading & ~mask))

    # ALL THE REMAINING LINES IN THIS FUNCTION ARE FOR DEBUG PURPOSES AND CAN BE REMOVED,
    # READ OUT THE IO DIRECTION REGISTER TO VERIFY CONTENTS.
    # We should get 0x7F. Leaving this code in until we get the rev 2 boards.
    logger.debug('reading register')
    vec.Start()
    vec.Write(WRITE_23017 + IODIRA_23017)
    vec.Stop()
    vec.Start()
    vec.Write(READ_23017)
    vec.SendNacks()
    direction = vec.Read(1)
    vec.Stop()
    vec.SendAcks()
    print 'MCP23017 IO Direction Port A Register: 0x%0.2X' % ord(direction)
    _close_i2c(vec_a)
    _close_i2c(vec_b)


def _close_i2c(vec):
    """Close the i2c port on the VEC USB."""
    vec.Close()


def _close_vec_i2c(vec, gpio):
    """Close the i2c port on the VEC USB."""
    vec.Stop()
    vec.Close()
    gpio.Close()


def _open_led_i2c():
    """Open the i2c port on the VEC USB for led controller operations."""
    # Construct string to set channel number to 3
    channel_str = PCA9546_WRITE_ADDRESS + '\x08'

    # Port A I2C for PCA9546A
    vec = MPSSE()
    vec.Open(0x0403, 0x6011, I2C, ONE_HUNDRED_KHZ, MSB, IFACE_A)

    # Port B I2C for debug leds (don't need the io expander for the LED Port)
    gpio = MPSSE()
    gpio.Open(0x0403, 0x6011, I2C, ONE_HUNDRED_KHZ, MSB, IFACE_B)

    # Set RESET line on PCA9546A to high to activate switch
    vec.PinHigh(GPIOL0)

    # Set channel on PCA9546A to 3 for PCA9632 (LED Port) Keep in mind this
    # also is connected to the MAX11608
    vec.Start()
    vec.Write(channel_str)
    vec.Stop()

    # verify channel was set.
    vec.Start()
    vec.Write(PCA9546_READ_ADDRESS)
    vec.SendNacks()
    reg = vec.Read(1)
    vec.Stop()
    reg = ord(reg)
    if reg != 0x08:
        raise ValueError('Failed to set PCA9546A Control Register to 0x08. Is 0x{:02x}'.format(reg))

    vec.SendAcks()
    vec.Start()

    # Configure PCA9632 for our setup (IE totem pole with inverted driver output, etc)
    # Writes to Mode 1 and Mode 2 Registers
    vec.Start()
    vec.Write('\xC4\x80\x00\x35')
    vec.Stop()

    return vec, gpio


def _close_led_i2c(vec, gpio):
    """Close the i2c port on the VEC USB."""
    vec.Close()
    gpio.Close()


def get_channel_ordinal(channel):
    """ The differential pressure sensors have a channel setting that uses a
    bit shift.

    Args:
        channel (int): the channel number.

    Raises:
        ValueError on invalid channel.
    """
    channels = [1, 2, 4, 8, 16, 32, 64, 128]
    return channels.index(channel)


def get_channel_from_ordinal(ordinal):
    """ The differential pressure sensors have a channel setting that uses a
    bit shift.

    Args:
        ordinal (int): the index into the channels list.

    Raises:
        ValueError on invalid channel.
    """
    channels = [1, 2, 4, 8, 16, 32, 64, 128]
    return channels[ordinal]


def _normalize_differential_pressure_result(channel, readings):
    """ Normalize the raw differential pressure readings to produce a result.

    The raw readings will vary wildly due to turbulence.

    Args:
        channel: the I2C channel of the sensor.
        readings: the raw differential pressure readings from the sensor.
    """
    # Dict for aggregating and logging.
    result_stats = {'sample_count': DIFFERENTIAL_PRESSURE_READ_COUNT}
    result_stats['raw_mean'], result_stats['raw_stddev'] = stats.std_dev(readings)

    if readings is None:
        # Bad sensor read. Don't fail the fan_sensors web route.
        result_stats['raw_mean'] = None
        result_stats['raw_stddev'] = None
        result_stats['remove_count'] = None
        result_stats['outliers'] = None
        result_stats['list'] = None
        result_stats['mean'] = None
        result_stats['stddev'] = None
        return result_stats

    # Remove outliers.
    readings_copy = copy.deepcopy(readings)
    outlier_results = stats.remove_outliers_percent(readings_copy, .3)

    # Get the results:
    result_stats['remove_count'] = outlier_results['removed']
    result_stats['outliers'] = outlier_results['outliers']
    result_stats['list'] = outlier_results['list']
    result_stats['mean'] = outlier_results['mean']
    result_stats['stddev'] = outlier_results['stddev']

    logger.debug('Differential Pressure Reading channel {}: result_stats{}'.format(
        channel, result_stats))

    return result_stats


def _read_differential_pressure_channel(vec, channel):
    """ Internal common code for dp reads. This part handles the per channel bus
    reads.

    Args:
        vec: handle for reading.
        channel: the I2C channel to read.

    Returns:
        list: a list of differential pressure readings in Pascals. These will
            vary widely due to turbulence.
    """
    result = []

    # Set channel
    # Convert channel number to string and add to address.
    channel_str = PCA9546_WRITE_ADDRESS + chr(channel)

    vec.Start()
    vec.Write(channel_str)
    vec.Stop()

    # verify channel was set
    vec.Start()
    vec.Write(PCA9546_READ_ADDRESS)
    vec.SendNacks()

    channel_reading = vec.Read(1)
    channel_read = conversions.unpack_byte(channel_reading)
    if channel_read == channel:
        logger.debug('OK: Set differential pressure channel to {}'.format(channel_read))
    else:
        logger.error(
            'FAILED Setting differential pressure channel to {}, channel is {}'.format(
                channel, channel_read))

    vec.Stop()
    vec.SendAcks()

    # Read DP sensor connected to the set channel.
    for _ in range(DIFFERENTIAL_PRESSURE_READ_COUNT):

        # Read DPS sensor connected to the set channel
        vec.SendAcks()
        vec.Start()
        vec.Write('\x80\xF1')
        vec.Start()
        vec.Write('\x81')

        # Give DPS610 time for the conversion since clock stretching is not implemented
        # 5ms seems to work fine, if wonkyness happens may have to increase.
        # So far this seems fine for 9 bit resolution.
        time.sleep(0.001)

        # Read the three bytes out of the DPS sensor (two data bytes and crc)
        sensor_data = vec.Read(3)
        vec.Stop()

        if _crc8(sensor_data):
            result.append(conversions.differential_pressure_sdp610(sensor_data, 0))
        else:
            logger.error('CRC failure reading Differential Pressure')

    if len(result) == 0:
        logger.error('No differential pressure readings for channel {}'.format(channel))
        return None
    return result


def configure_differential_pressure(channel):
    """ Configure the differential pressure sensor for 9 bit resolution.

    Default is 12. This needs to be done once per power up. For our case we call this
    on synse container startup.

    Args:
        channel: the channel to configure.
    """
    logger.debug('Configuring Differential Pressure sensor on channel {}'.format(channel))

    # Port A I2C for PCA9546A
    vec = MPSSE()
    vec.Open(0x0403, 0x6011, I2C, ONE_HUNDRED_KHZ, MSB, IFACE_A)

    # Set RESET line on PCA9546A to high to activate switch
    vec.PinHigh(GPIOL0)
    time.sleep(0.001)

    channel_str = PCA9546_WRITE_ADDRESS + chr(channel)
    logger.debug('PCA9546_WRITE_ADDRESS is: {}'.format(hexlify(PCA9546_WRITE_ADDRESS)))
    logger.debug('channel_str is: {}'.format(hexlify(channel_str)))

    vec.Start()
    vec.Write(channel_str)
    vec.Stop()

    logger.debug('PCA9546_READ_ADDRESS is: {}'.format(hexlify(PCA9546_READ_ADDRESS)))

    # verify channel was set
    vec.Start()
    vec.Write(PCA9546_READ_ADDRESS)
    vec.SendNacks()
    reg = vec.Read(1)
    vec.Stop()
    vec.SendAcks()

    logger.debug('PCA9546A Control Register: 0x{:02x}'.format(ord(reg)))

    # Configure Sensor
    # In the application note for changing measurement resolution three things must be met.
    # 1. Read the advanced user register.
    # 2. Define the new register entry according to the desired resolution.
    # 3. Write the new value to the advanced register.
    vec.Start()
    vec.Write('\x80\xE5')
    vec.Start()
    vec.Write('\x81')

    # At this point the sensor needs to hold the master but the FT4232 doesn't do clock stretching.
    time.sleep(0.001)  # This stays at 1 ms regardless of sensor resolution.

    # Read the three bytes out of the DPS sensor (two data bytes and crc)
    sensor_data = vec.Read(3)
    logger.debug('Raw sensor_data: {}'.format(hexlify(sensor_data)))
    vec.Stop()

    # write new value for 9 bit resolution (0b000 for bits 9 - 11)
    if _crc8(sensor_data):

        # Hardcoded 9 bit sensor resolution.
        sensor_int = 0x7182

        msb = sensor_int >> 8
        lsb = sensor_int & 0xFF
        register_str = '\x80\xE4' + chr(msb) + chr(lsb)

        vec.Start()
        vec.Write(register_str)
        vec.Stop()
        logger.debug('Configured DP Sensor on channel {} for 9 bit resolution.'.format(channel))
        rc = 0
    else:
        logger.error('CRC failed configuring DP Sensor.')
        rc = 1

    vec.Stop()
    vec.Close()
    return rc


def read_differential_pressure(channel):
    """ This will read a single differential pressure sensor from the
    CEC board.

    Args:
        channel: the channel to read.

    Returns:
        The differential pressure in Pascals, or None on failure.
    """
    vec, gpio = _open_vec_i2c()

    if vec.GetAck() == ACK:
        # If we got an ack then switch is there.
        vec.SendNacks()
        vec.Read(1)
        vec.Stop()
        vec.SendAcks()

        readings = _read_differential_pressure_channel(vec, channel)

        channel_str = PCA9546_WRITE_ADDRESS + '\x00'
        vec.Start()
        vec.Write(channel_str)
        vec.Stop()

    else:
        logger.error('No ACK from PCA9546A')
        _close_vec_i2c(vec, gpio)
        return None

    _close_vec_i2c(vec, gpio)

    # Now that the bus is closed, normalize the results.
    normalized_result = _normalize_differential_pressure_result(channel, readings)
    if normalized_result is None:
        return None  # Read of bad sensor.
    return normalized_result['mean']


def read_differential_pressures(count):
    """ This will read count number of differential pressure sensors from the
    CEC board.

    Args:
        count (int): the number of differential pressure sensors to read.

    Returns:
        list: an array of differential pressure sensor readings in Pascals.
            The array index will be the same as the channel in the synse i2c sdp-610
            differential pressure sensor configuration. None is returned on failure.
    """

    start_time = datetime.datetime.now()
    vec, gpio = _open_vec_i2c()
    result = []
    raw_result = []

    if vec.GetAck() == ACK:
        # If we got an ack then switch is there.
        vec.SendNacks()
        vec.Read(1)
        vec.Stop()
        vec.SendAcks()

        # Cycle through the count number of sensors connected to each channel
        # on the PCA9546A.
        channel = 1
        for _ in range(count):
            raw_result.append(_read_differential_pressure_channel(vec, channel))
            # set the next channel
            channel <<= 1

        channel_str = PCA9546_WRITE_ADDRESS + '\x00'
        vec.Start()
        vec.Write(channel_str)
        vec.Stop()
    else:
        logger.error('No ACK from PCA9546A')
        _close_vec_i2c(vec, gpio)
        return None

    _close_vec_i2c(vec, gpio)

    # Now that the bus is closed, normalize the results.
    for index, raw in enumerate(raw_result):
        channel = get_channel_from_ordinal(index)
        normalized = _normalize_differential_pressure_result(channel, raw)
        if normalized is None:
            return None  # Read of bad sensor.
        result.append(normalized['mean'])

    end_time = datetime.datetime.now()
    logger.debug('Differential Pressure Read time: {} ms'.format(
        (end_time - start_time).total_seconds() * 1000))
    for reading in result:
        logger.debug('Differential Pressure Reading:   {} Pa'.format(reading))
    return result


# PCA9632 LED Controller constants.
PCA9632_WRITE = chr(0xC4)
PCA9632_READ = chr(0xC5)
PCA9632_LEDOUT_BLINK = chr(0x3F)        # Brightness controlled by PWMx register. Blinking controlled by GRPPWM register
PCA9632_LEDOUT_STEADY = chr(0x2A)       # Brightness controlled by PWMx register.
PCA9632_LEDOUT_OFF = chr(0x00)          # Led output off.
PCA9632_GRPPWM_FULL = chr(0xFC)         # 98.4 % group duty cycle. 64-step duty cycle resolution.
PCA9632_GRPFREQ_2S_BLINK = chr(0x2F)    # Blink all LEDs at 2 second frequency. (one second on, one second off)

# register options
PCA9632_AUTO_INCR = chr(0x80)   # Enables Auto-Increment, Mode register 1.

# register map
PCA9632_MODE1 = chr(0x00)    # Mode register 1
PCA9632_MODE2 = chr(0x01)    # Mode register 2
PCA9632_PWM0 = chr(0x02)     # brightness control LED0
PCA9632_PWM1 = chr(0x03)     # brightness control LED1
PCA9632_PWM2 = chr(0x04)     # brightness control LED2
PCA9632_PWM3 = chr(0x05)     # brightness control LED3 (Unused if I understand correctly.)
PCA9632_GRPPWM = chr(0x06)   # group duty cycle control
PCA9632_GRPFREQ = chr(0x07)  # group frequency
PCA9632_LEDOUT = chr(0x08)   # LED output state


def read_led():
    """ Read the led state from the led controller. There is one led controller
    per wedge.

    Returns:
        tuple: state, color, and blink.
            state is on or off.
            color is a 3 byte RGB color.
            blink is blink or steady.
    """
    vec, gpio = _open_led_i2c()

    # Read out color. PWM0 register. (Register 2)
    vec.Start()
    # write, auto increment, start at PWM0 register.
    vec.Write(PCA9632_WRITE + chr(ord(PCA9632_AUTO_INCR) | ord(PCA9632_PWM0)))
    vec.Stop()

    vec.Start()
    vec.Write(PCA9632_READ)    # start a read
    color = vec.Read(2)  # read three bytes to read PWM0, PWM1, PWM2
    vec.SendNacks()
    c2 = vec.Read(1)
    color += c2
    vec.Stop()

    # Read out Register 0x08 (LEDOUT).
    vec.Start()
    vec.Write(PCA9632_WRITE + PCA9632_LEDOUT)
    vec.Stop()

    vec.Start()
    vec.Write(PCA9632_READ)    # start a read
    ledout = vec.Read(1)
    vec.Stop()

    _close_led_i2c(vec, gpio)

    ledout = ord(ledout)
    logger.debug('PCA9632 Register 0x08: 0x{:02x}'.format(ledout))
    ledout = chr(ledout)
    color = hexlify(color)

    # Convert state and blink from the LEDOUT state register.
    if ledout == PCA9632_LEDOUT_OFF:
        state = 'off'
        blink = 'steady'
    elif ledout == PCA9632_LEDOUT_STEADY:
        state = 'on'
        blink = 'steady'
    elif ledout == PCA9632_LEDOUT_BLINK:
        state = 'on'
        blink = 'blink'
    else:
        raise ValueError('Unknown LEDOUT state 0x{}.'.format(hexlify(ledout)))

    return state, color, blink


def check_led_write_parameters(state, color=None, blink_state=None):
    """ Check parameter validity.

    Args:
        state (str): on or off
        color (int): a 3-byte RGB color. None is fine for off.
        blink_state (str): blink, steady, or no_override. None is fine for off.
    """
    # Parameter checks.
    if state not in ['on', 'off']:
        raise ValueError('Invalid state parameter {}.'.format(state))
    if state == 'on' and (color is None or blink_state is None):
        raise ValueError('Color and blink_state must be specified when state is on.')
    if blink_state not in ['blink', 'steady', 'no_override', None]:
        raise ValueError('Invalid blink_state {}.'.format(blink_state))
    if color is not None and (color < 0 or color > 0xFFFFFF):
        raise ValueError('Color {:0x} out of range. 0 <= color < 0xFFFFFF'.format(color))
    elif color is not None and blink_state is None:
        raise ValueError('color is not None and blink_state is None')
    elif color is None and blink_state is not None:
        raise ValueError('color is None and blink_state is not None')


def write_led(state, color=None, blink_state=None):
    """ Set the led state.

    Args:
        state (etd): on or off
        color (int | str): a 3-byte RGB color or hex string. None is fine for off.
        blink_state (str): blink, steady, or no_override. None is fine for off.
    """

    # If color is a string, assume hex and convert to int.
    if isinstance(color, basestring):
        color = int(color, 16)

    # Parameter checks.
    check_led_write_parameters(state, color, blink_state)

    vec, gpio = _open_led_i2c()

    if state == 'off':
        # Turn off all outputs with the LEDOUT register.
        vec.Start()
        # write, LEDOUT register, 0x00 for off.
        vec.Write(PCA9632_WRITE + PCA9632_LEDOUT + PCA9632_LEDOUT_OFF)
        vec.Stop()
    else:
        # Set the colors.
        color_bytes = struct.pack('>L', color)  # Pack color to three bytes.
        color_bytes = color_bytes[1:]
        to_write = PCA9632_WRITE + chr(ord(PCA9632_AUTO_INCR) | ord(PCA9632_PWM0)) + color_bytes

        vec.Start()
        vec.Write(to_write)
        vec.Stop()

        # Set the blink state.
        if blink_state == 'steady':
            vec.Start()
            vec.Write(PCA9632_WRITE + PCA9632_LEDOUT + PCA9632_LEDOUT_STEADY)
            vec.Stop()
        elif blink_state == 'blink':
            vec.Start()
            # Set group period for 1 second and group duty cycle of 50% (controls the blinking)
            # This writes to registers 6 and 7 by setting the increment bit in the register (0x86)
            vec.Write(PCA9632_WRITE + chr(ord(PCA9632_AUTO_INCR) | ord(PCA9632_GRPPWM)) +
                      '\x80' + PCA9632_GRPFREQ_2S_BLINK)
            vec.Stop()

            # Set the output to enable the blinking.
            vec.Start()
            vec.Write(PCA9632_WRITE + PCA9632_LEDOUT + PCA9632_LEDOUT_BLINK)
            vec.Stop()

    _close_led_i2c(vec, gpio)


def _get_thermistor_registers(device_name):
    """Get the read and write registers for the max116xx A/D converter attached
    to a thermistor.
    :param device_name: max-11608 and max-11610 are supported.
    :returns: read_register, write_register.
    :raises ValueError if device_name is not supported"""
    if device_name == 'max-11610':
        read_register = '\x6B'
        write_register = '\x6A'
    elif device_name == 'max-11608':
        read_register = '\x67'
        write_register = '\x66'
    else:
        raise ValueError('Unknown device_name {}.'.format(device_name))

    return read_register, write_register


def read_thermistors(count, device_name):
    """This will read count number of thermistors from the CEC board.
    :param count: The number of thermistors to read.
    :param device_name: max-11608 and max-11610 are supported.
    :returns: An array of thermistor readings in degrees Celsius. The array
    index will be the same as the channel in the synse i2c max-11608 thermistor
    configuration."""
    read_register, write_register = _get_thermistor_registers(device_name)

    # construct channel 3 command based on address
    channel_3 = PCA9546_WRITE_ADDRESS + '\x08'

    vec, gpio = _open_vec_i2c()

    if vec.GetAck() == ACK:

        # if we got an ack then slave is there
        vec.SendNacks()
        vec.Read(1)

        vec.SendAcks()
        vec.Stop()

        # Set channel to 3 for MAX11608
        vec.Start()
        vec.Write(channel_3)
        vec.Stop()

        # verify channel was set
        vec.Start()
        vec.Write(PCA9546_READ_ADDRESS)
        vec.SendNacks()
        vec.Read(1)
        vec.Stop()

        # Configure MAX116xx
        # There are two registers to write to however there is no address.
        # Bit 7 determines which register gets written; 0 = Configuration byte,
        # 1 = Setup byte
        vec.SendAcks()
        vec.Start()

        # Following the slave address write 0xD2 for setup byte and 0x0F for configuration
        # byte. See tables 1 and 2 in MAX116xx for byte definitions but basically sets up
        # for an internal reference and do an a/d conversion all channels
        vec.Write(write_register + '\xD2\x0F')

        # Initiating a read starts the conversion
        vec.Start()
        vec.Write(read_register)

        # delay for conversion since the libmpsse can't do clock stretching
        time.sleep(0.010)

        # Read the count number of channels (2 bytes per channel)
        ad_reading = vec.Read(count * 2)

    else:
        logger.error('No ACK from thermistors.')
        _close_vec_i2c(vec, gpio)
        return None

    _close_vec_i2c(vec, gpio)

    # Convert the raw reading for each thermistor.
    result = []
    if ad_reading:
        for x in range(count):
            index = x * 2
            temperature = conversions.convert_thermistor_reading(
                ad_reading, index, device_name)
            result.append(temperature)

    return result


def _crc8(data):
    """ CRC check on the packet.

    Returns:
        bool: True on success, False on failure.
    """
    polynomial = 0x131
    crc = 0
    for x in range(len(data) - 1):
        crc ^= ord(data[x])
        for _ in range(8):
            if crc & 0x80:
                crc = (crc << 1) ^ polynomial
            else:
                crc = (crc << 1)

    return crc == ord(data[2])
