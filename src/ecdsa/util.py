from __future__ import division

import os
import math
import binascii
from hashlib import sha256
from six import PY3, int2byte, b, next
from . import der

# RFC5480:
#   The "unrestricted" algorithm identifier is:
#     id-ecPublicKey OBJECT IDENTIFIER ::= {
#       iso(1) member-body(2) us(840) ansi-X9-62(10045) keyType(2) 1 }

oid_ecPublicKey = (1, 2, 840, 10045, 2, 1)
encoded_oid_ecPublicKey = der.encode_oid(*oid_ecPublicKey)


def bit_length(num):
    # http://docs.python.org/dev/library/stdtypes.html#int.bit_length
    s = bin(num)  # binary representation:  bin(-37) --> '-0b100101'
    s = s.lstrip('-0b')  # remove leading zeros and minus sign
    return len(s)  # len('100101') --> 6


def orderlen(order):
    return (1+len("%x" % order))//2  # bytes


def randrange(order, entropy=None):
    """Return a random integer k such that 1 <= k < order, uniformly
    distributed across that range. For simplicity, this only behaves well if
    'order' is fairly close (but below) a power of 256. The try-try-again
    algorithm we use takes longer and longer time (on average) to complete as
    'order' falls, rising to a maximum of avg=512 loops for the worst-case
    (256**k)+1 . All of the standard curves behave well. There is a cutoff at
    10k loops (which raises RuntimeError) to prevent an infinite loop when
    something is really broken like the entropy function not working.

    Note that this function is not declared to be forwards-compatible: we may
    change the behavior in future releases. The entropy= argument (which
    should get a callable that behaves like os.urandom) can be used to
    achieve stability within a given release (for repeatable unit tests), but
    should not be used as a long-term-compatible key generation algorithm.
    """
    # we could handle arbitrary orders (even 256**k+1) better if we created
    # candidates bit-wise instead of byte-wise, which would reduce the
    # worst-case behavior to avg=2 loops, but that would be more complex. The
    # change would be to round the order up to a power of 256, subtract one
    # (to get 0xffff..), use that to get a byte-long mask for the top byte,
    # generate the len-1 entropy bytes, generate one extra byte and mask off
    # the top bits, then combine it with the rest. Requires jumping back and
    # forth between strings and integers a lot.

    if entropy is None:
        entropy = os.urandom
    assert order > 1
    bytes = orderlen(order)
    dont_try_forever = 10000  # gives about 2**-60 failures for worst case
    while dont_try_forever > 0:
        dont_try_forever -= 1
        candidate = string_to_number(entropy(bytes)) + 1
        if 1 <= candidate < order:
            return candidate
        continue
    raise RuntimeError("randrange() tried hard but gave up, either something"
                       " is very wrong or you got realllly unlucky. Order was"
                       " %x" % order)


class PRNG:
    # this returns a callable which, when invoked with an integer N, will
    # return N pseudorandom bytes. Note: this is a short-term PRNG, meant
    # primarily for the needs of randrange_from_seed__trytryagain(), which
    # only needs to run it a few times per seed. It does not provide
    # protection against state compromise (forward security).
    def __init__(self, seed):
        self.generator = self.block_generator(seed)

    def __call__(self, numbytes):
        a = [next(self.generator) for i in range(numbytes)]

        if PY3:
            return bytes(a)
        else:
            return "".join(a)

    def block_generator(self, seed):
        counter = 0
        while True:
            for byte in sha256(("prng-%d-%s" % (counter, seed)).encode()).digest():
                yield byte
            counter += 1


def randrange_from_seed__overshoot_modulo(seed, order):
    # hash the data, then turn the digest into a number in [1,order).
    #
    # We use David-Sarah Hopwood's suggestion: turn it into a number that's
    # sufficiently larger than the group order, then modulo it down to fit.
    # This should give adequate (but not perfect) uniformity, and simple
    # code. There are other choices: try-try-again is the main one.
    base = PRNG(seed)(2 * orderlen(order))
    number = (int(binascii.hexlify(base), 16) % (order - 1)) + 1
    assert 1 <= number < order, (1, number, order)
    return number


def lsb_of_ones(numbits):
    return (1 << numbits) - 1


def bits_and_bytes(order):
    bits = int(math.log(order - 1, 2) + 1)
    bytes = bits // 8
    extrabits = bits % 8
    return bits, bytes, extrabits


# the following randrange_from_seed__METHOD() functions take an
# arbitrarily-sized secret seed and turn it into a number that obeys the same
# range limits as randrange() above. They are meant for deriving consistent
# signing keys from a secret rather than generating them randomly, for
# example a protocol in which three signing keys are derived from a master
# secret. You should use a uniformly-distributed unguessable seed with about
# curve.baselen bytes of entropy. To use one, do this:
#   seed = os.urandom(curve.baselen) # or other starting point
#   secexp = ecdsa.util.randrange_from_seed__trytryagain(sed, curve.order)
#   sk = SigningKey.from_secret_exponent(secexp, curve)

def randrange_from_seed__truncate_bytes(seed, order, hashmod=sha256):
    # hash the seed, then turn the digest into a number in [1,order), but
    # don't worry about trying to uniformly fill the range. This will lose,
    # on average, four bits of entropy.
    bits, _bytes, extrabits = bits_and_bytes(order)
    if extrabits:
        _bytes += 1
    base = hashmod(seed).digest()[:_bytes]
    base = "\x00" * (_bytes - len(base)) + base
    number = 1 + int(binascii.hexlify(base), 16)
    assert 1 <= number < order
    return number


def randrange_from_seed__truncate_bits(seed, order, hashmod=sha256):
    # like string_to_randrange_truncate_bytes, but only lose an average of
    # half a bit
    bits = int(math.log(order - 1, 2) + 1)
    maxbytes = (bits + 7) // 8
    base = hashmod(seed).digest()[:maxbytes]
    base = "\x00" * (maxbytes - len(base)) + base
    topbits = 8 * maxbytes - bits
    if topbits:
        base = int2byte(ord(base[0]) & lsb_of_ones(topbits)) + base[1:]
    number = 1 + int(binascii.hexlify(base), 16)
    assert 1 <= number < order
    return number


def randrange_from_seed__trytryagain(seed, order):
    # figure out exactly how many bits we need (rounded up to the nearest
    # bit), so we can reduce the chance of looping to less than 0.5 . This is
    # specified to feed from a byte-oriented PRNG, and discards the
    # high-order bits of the first byte as necessary to get the right number
    # of bits. The average number of loops will range from 1.0 (when
    # order=2**k-1) to 2.0 (when order=2**k+1).
    assert order > 1
    bits, bytes, extrabits = bits_and_bytes(order)
    generate = PRNG(seed)
    while True:
        extrabyte = b("")
        if extrabits:
            extrabyte = int2byte(ord(generate(1)) & lsb_of_ones(extrabits))
        guess = string_to_number(extrabyte + generate(bytes)) + 1
        if 1 <= guess < order:
            return guess


def number_to_string(num, order):
    l = orderlen(order)
    fmt_str = "%0" + str(2 * l) + "x"
    string = binascii.unhexlify((fmt_str % num).encode())
    assert len(string) == l, (len(string), l)
    return string


def number_to_string_crop(num, order):
    l = orderlen(order)
    fmt_str = "%0" + str(2 * l) + "x"
    string = binascii.unhexlify((fmt_str % num).encode())
    return string[:l]


def string_to_number(string):
    return int(binascii.hexlify(string), 16)


def string_to_number_fixedlen(string, order):
    l = orderlen(order)
    assert len(string) == l, (len(string), l)
    return int(binascii.hexlify(string), 16)


# these methods are useful for the sigencode= argument to SK.sign() and the
# sigdecode= argument to VK.verify(), and control how the signature is packed
# or unpacked.

def sigencode_strings(r, s, order):
    r_str = number_to_string(r, order)
    s_str = number_to_string(s, order)
    return (r_str, s_str)


def sigencode_string(r, s, order):
    """
    Encode the signature to raw format (:term:`raw encoding`)

    It's expected that this function will be used as a `sigencode=` parameter
    in :func:`ecdsa.keys.SigningKey.sign` method.

    :param int r: first parameter of the signature
    :param int s: second parameter of the signature
    :param int order: the order of the curve over which the signature was
        computed

    :return: raw encoding of ECDSA signature
    :rtype: bytes
    """
    # for any given curve, the size of the signature numbers is
    # fixed, so just use simple concatenation
    r_str, s_str = sigencode_strings(r, s, order)
    return r_str + s_str


def sigencode_der(r, s, order):
    """
    Encode the signature into the ECDSA-Sig-Value structure using :term:`DER`.

    Encodes the signature to the following :term:`ASN.1` structure::

        Ecdsa-Sig-Value ::= SEQUENCE {
            r       INTEGER,
            s       INTEGER
        }

    It's expected that this function will be used as a `sigencode=` parameter
    in :func:`ecdsa.keys.SigningKey.sign` method.

    :param int r: first parameter of the signature
    :param int s: second parameter of the signature
    :param int order: the order of the curve over which the signature was
        computed

    :return: DER encoding of ECDSA signature
    :rtype: bytes
    """
    return der.encode_sequence(der.encode_integer(r), der.encode_integer(s))


# canonical versions of sigencode methods
# these enforce low S values, by negating the value (modulo the order) if above order/2
# see CECKey::Sign() https://github.com/bitcoin/bitcoin/blob/master/src/key.cpp#L214
def sigencode_strings_canonize(r, s, order):
    if s > order / 2:
        s = order - s
    return sigencode_strings(r, s, order)


def sigencode_string_canonize(r, s, order):
    if s > order / 2:
        s = order - s
    return sigencode_string(r, s, order)


def sigencode_der_canonize(r, s, order):
    if s > order / 2:
        s = order - s
    return sigencode_der(r, s, order)


class MalformedSignature(Exception):
    """
    Raised by decoding functions when the signature is malformed.

    Malformed in this context means that the relevant strings or integers
    do not match what a signature over provided curve would create. Either
    because the byte strings have incorrect lengths or because the encoded
    values are too large.
    """

    pass


def sigdecode_string(signature, order):
    """
    Decoder for :term:`raw encoding`  of ECDSA signatures.

    raw encoding is a simple concatenation of the two integers that comprise
    the signature, with each encoded using the same amount of bytes depending
    on curve size/order.

    It's expected that this function will be used as the `sigdecode=`
    parameter to the :func:`ecdsa.keys.VerifyingKey.verify` method.

    :param signature: encoded signature
    :type signature: bytes like object
    :param order: order of the curve over which the signature was computed
    :type order: int

    :raises MalformedSignature: when the encoding of the signature is invalid

    :return: tuple with decoded 'r' and 's' values of signature
    :rtype: tuple of ints
    """
    l = orderlen(order)
    if not len(signature) == 2 * l:
        raise MalformedSignature(
            "Invalid length of signature, expected {0} bytes long, "
            "provided string is {1} bytes long"
            .format(2 * l, len(signature)))
    r = string_to_number_fixedlen(signature[:l], order)
    s = string_to_number_fixedlen(signature[l:], order)
    return r, s


def sigdecode_strings(rs_strings, order):
    """
    Decode the signature from two strings.

    First string needs to be a big endian encoding of 'r', second needs to
    be a big endian encoding of the 's' parameter of an ECDSA signature.

    It's expected that this function will be used as the `sigdecode=`
    parameter to the :func:`ecdsa.keys.VerifyingKey.verify` method.

    :param list rs_strings: list of two bytes-like objects, each encoding one
        parameter of signature
    :param int order: order of the curve over which the signature was computed

    :raises MalformedSignature: when the encoding of the signature is invalid

    :return: tuple with decoded 'r' and 's' values of signature
    :rtype: tuple of ints
    """
    if not len(rs_strings) == 2:
        raise MalformedSignature(
            "Invalid number of strings provided: {0}, expected 2"
            .format(len(rs_strings)))
    (r_str, s_str) = rs_strings
    l = orderlen(order)
    if not len(r_str) == l:
        raise MalformedSignature(
            "Invalid length of first string ('r' parameter), "
            "expected {0} bytes long, provided string is {1} bytes long"
            .format(l, len(r_str)))
    if not len(s_str) == l:
        raise MalformedSignature(
            "Invalid length of second string ('s' parameter), "
            "expected {0} bytes long, provided string is {1} bytes long"
            .format(l, len(s_str)))
    r = string_to_number_fixedlen(r_str, order)
    s = string_to_number_fixedlen(s_str, order)
    return r, s


def sigdecode_der(sig_der, order):
    """
    Decoder for DER format of ECDSA signatures.

    DER format of signature is one that uses the :term:`ASN.1` :term:`DER`
    rules to encode it as a sequence of two integers::

        Ecdsa-Sig-Value ::= SEQUENCE {
            r       INTEGER,
            s       INTEGER
        }

    It's expected that this function will be used as as the `sigdecode=`
    parameter to the :func:`ecdsa.keys.VerifyingKey.verify` method.

    :param sig_der: encoded signature
    :type sig_der: bytes like object
    :param order: order of the curve over which the signature was computed
    :type order: int

    :raises UnexpectedDER: when the encoding of signature is invalid

    :return: tuple with decoded 'r' and 's' values of signature
    :rtype: tuple of ints
    """
    # return der.encode_sequence(der.encode_integer(r), der.encode_integer(s))
    rs_strings, empty = der.remove_sequence(sig_der)
    if empty != b(""):
        raise der.UnexpectedDER("trailing junk after DER sig: %s" %
                                binascii.hexlify(empty))
    r, rest = der.remove_integer(rs_strings)
    s, empty = der.remove_integer(rest)
    if empty != b(""):
        raise der.UnexpectedDER("trailing junk after DER numbers: %s" %
                                binascii.hexlify(empty))
    return r, s
