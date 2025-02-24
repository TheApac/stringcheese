#!/usr/bin/env python3

import base64
import binascii
import gc
import sys
from argparse import ArgumentParser

from stringcheese.ahocorasick import *
from tqdm import tqdm

MAX_FLAG_LENGTH = 2000
CLOSING_CHAR = b'}'


def setup_parser():
    parser = ArgumentParser(description='Find flags automatically in '
                            'CTF challenges. This looks for flags '
                            'in the provided files using searches similar '
                            'to strings+grep, but works even if the flag is '
                            'transformed, e.g. encoded or xor-encrypted.',
                            add_help=False)

    parser.add_argument('--help', '-h', action='help', help='show this help '
                        'message and exit')

    parser.add_argument('pattern', type=str, help='the pattern you want to '
                        'search, e.g. FLAG{')

    parser.add_argument('--file', '-f', type=str, help='the file in which '
                        'to search for flags, stdin by default', default='-')

    parser.add_argument('--fast', help='skip the slow checks. Useful '
                        'on larger files but you may miss matches',
                        action='store_true')

    parser.add_argument('-v', '--verbose', help='increase output verbosity',
                        action='store_true')

    return parser


def identity_decoder(match):
    return match


def b64_decoder(match):
    try:
        # If padding is present, b64decode ignores leftover data and works
        return base64.b64decode(match)
    except:
        pass

    # Try to find a decodeable base64 string by trimming progressively
    for trim_len in range(len(match), -1, -1):
        if trim_len % 4 == 1:
            continue  # b64 is not compatible with this data length
        trim_match = match[:trim_len]

        # Add correct padding
        while len(trim_match) % 4:
            trim_match += b'='

        try:
            return base64.b64decode(trim_match)
        except:
            pass  # Decode failed. Ignore

    return None  # All decodes failed


def b32_decoder(match):
    try:
        # If padding is present, b32decode ignores leftover data and works
        return base64.b32decode(match)
    except:
        pass

    # Try to find a decodeable base32 string by trimming progressively
    for trim_len in range(len(match), -1, -1):
        if trim_len % 8 == 1:
            continue  # b32 is not compatible with this data length
        trim_match = match[:trim_len]

        # Add correct padding
        while len(trim_match) % 8:
            trim_match += b'='

        try:
            return base64.b32decode(trim_match)
        except:
            pass  # Decode failed. Ignore

    return None  # All decodes failed


def crypt_rot13(message):
    d = {}
    for c in (65, 97):
        for i in range(26):
            d[chr(i + c)] = chr((i + 13) % 26 + c)
    return str.encode("".join([d.get(chr(c), chr(c)) for c in message]))


def crypt_rot47(message):
    d = {}
    for letter in range(47):
        d[chr(letter + 33)] = chr((33 + letter) + 47)
    for letter in range(47, 94):
        d[chr(letter + 33)] = chr((33 + letter) - 47)
    return str.encode("".join([d.get(chr(c), chr(c)) for c in message]))


def codec_decoder_generator(codec):
    def codec_decoder(match):
        while match:
            try:
                return match.decode(codec).encode()
            except:
                pass
            match = match[:-1]
        return None
    return codec_decoder


def hex_decoder(match):
    # Ensure all bytes in the match are hex
    for i in range(len(match)):
        if match[i] not in b'0123456789abcdef':
            match = match[:i]
            break
    if len(match) % 2:
        match = match[:-1]
    return binascii.unhexlify(match)


def hex_bytes_decoder(match):
    for i in range(len(match)):
        if match[i] > 0xf:
            match = match[:i]
            break
    if len(match) % 2:
        match = match[:-1]
    return bytes(match[i] << 4 | match[i+1] for i in range(0, len(match), 2))


def bitstring_to_bytes(bitstring):
    return int(bitstring, 2).to_bytes(len(bitstring) // 8, byteorder='big')


def binary_decoder(match):
    for i in range(len(match)):
        if match[i] not in b'01':
            match = match[:i]
            break
    while len(match) % 8:
        match = match[:-1]
    return bitstring_to_bytes(match)


def binary_bytes_decoder(match):
    for i in range(len(match)):
        if match[i] > 1:
            match = match[:i]
            break
    while len(match) % 8:
        match = match[:-1]
    bin_converted = bytes(ord('0')+x for x in match)
    return bitstring_to_bytes(bin_converted)


def build_automaton(pattern):
    automaton = Automaton()

    # identity match
    automaton.add_word(pattern, (pattern, 'ASCII', identity_decoder))

    # base64 match
    b64pattern = base64.b64encode(pattern).rstrip(b'=')
    if len(b64pattern) % 3:
        b64pattern = b64pattern[:-1]
    automaton.add_word(b64pattern, (b64pattern, 'base64', b64_decoder))

    b32pattern = base64.b32encode(pattern).rstrip(b'=')
    if len(b32pattern) % 7:
        b32pattern = b32pattern[:-1]
    automaton.add_word(b32pattern, (b32pattern, 'base32', b32_decoder))

    # codec match
    for codec in ('utf-16', 'utf-16-be', 'utf-16-le',
                  'utf-32', 'utf-32-be', 'utf-32-le'):
        codec_pattern = pattern.decode().encode(codec)
        codec_decoder = codec_decoder_generator(codec)
        automaton.add_word(codec_pattern, (codec_pattern, codec, codec_decoder))

    # xor match
    for xorval in range(1, 256):
        xor_pattern = bytes(xorval ^ x for x in pattern)
        xor_decoder = lambda s, xorval=xorval: bytes(xorval ^ x for x in s)
        automaton.add_word(xor_pattern,
                           (xor_pattern, f'XOR_{xorval}', xor_decoder))

    # hex match
    hex_pattern = binascii.hexlify(pattern)
    automaton.add_word(hex_pattern, (hex_pattern, 'hex', hex_decoder))

    # raw hex match (bytes are \x00 through \x0f)
    raw_hex_pattern = bytes(int(x) for x in hex_pattern)
    automaton.add_word(raw_hex_pattern,
                       (raw_hex_pattern, 'raw_hex', hex_bytes_decoder))

    # binary match
    bin_pattern = ''.join(f'{pb:08b}' for pb in pattern).encode()
    automaton.add_word(bin_pattern, (bin_pattern, 'binary', binary_decoder))

    # rot13 match
    raw_rot13_pattern = crypt_rot13(pattern)
    automaton.add_word(raw_rot13_pattern,
                       (raw_rot13_pattern, 'raw_rot13', crypt_rot13))

    # rot47 match
    raw_rot47_pattern = crypt_rot47(pattern)
    automaton.add_word(raw_rot47_pattern,
                       (raw_rot47_pattern, 'raw_rot47', crypt_rot47))

    # raw binary match
    raw_bin_pattern = bytes(int(x) for x in bin_pattern)
    automaton.add_word(raw_bin_pattern,
                       (raw_bin_pattern, 'raw_binary', binary_bytes_decoder))

    # TODO: various ciphers, etc

    automaton.make_automaton()
    return automaton


def postprocess_match(raw_match):
    # Return a printable prefix of the match, ending at } if found
    for i, match_byte in enumerate(raw_match):
        if match_byte < 32 or match_byte > 126:
            raw_match = raw_match[:i]
            break

    if CLOSING_CHAR in raw_match:
        raw_match = raw_match.split(CLOSING_CHAR)[0] + CLOSING_CHAR

    return raw_match.decode()


def generate_haystacks(base_haystack, fast):
    yield base_haystack, 'stream'
    nb_steps = 33 if not fast else 8
    for step in range(2, nb_steps):
        for startpos in range(step):
            yield base_haystack[startpos::step], f'stream[{startpos}::{step}]'

    yield base_haystack[::-1], 'reversed stream'

    # TODO : add local xor for simple crackme challs? but may be slow


def extract_matches(automaton, filename, fast, verbose):
    if filename == '-':
        print('No filename provided, reading from stdin.')
        file_contents = sys.stdin.buffer.read()
    else:
        try:
            with open(filename, 'rb') as haystack_file:
                file_contents = haystack_file.read()
        except:
            print('Error opening file.')
            sys.exit(0)
    if fast:
        val = input("Warning, with --fast your files will be treated faster by ignoring some tests so you might miss "
                    "some flags. Do you wish to continue? (y/N) : ")
        if val != 'y':
            sys.exit(0)
    if len(file_contents) > 50000:
        val = input("This is a large file and may take a long time to be treated, do you wish to continue? (y/N) : ")
        if val != 'y':
            sys.exit(0)

    # TODO : decode file formats (zip, png pixels, etc)

    match_found = False

    # Compute the number of haystacks by counting them on a fake base
    n_haystacks = sum(1 for _ in generate_haystacks(b'fake haystack', fast))

    progress = tqdm(total=n_haystacks)
    for haystack, haystack_name in generate_haystacks(file_contents, fast):
        match_iter = list(automaton.iter(haystack))
        if match_iter:
            for end_index, (pattern, enc_desc, decoder) in match_iter:
                match_found = True
                start_index = end_index - len(pattern) + 1
                raw_match = haystack[start_index:start_index+MAX_FLAG_LENGTH]
                tqdm.write(f'MATCH FOUND! '
                           f'In {haystack_name}, using encoding {enc_desc}:')
                if verbose:
                    tqdm.write(binascii.hexlify(raw_match).decode())
                decoded_flag = decoder(raw_match)
                # tqdm.write(binascii.hexlify(decoded_flag).decode())
                processed_match = postprocess_match(decoded_flag)
                tqdm.write(processed_match)
                if fast:
                    sys.exit(0)

        # Keep memory consumption low
        del haystack
        gc.collect()
        progress.update(1)
    progress.close()

    if not match_found:
        print('No match found.')


def main():
    argparser = setup_parser()
    args = argparser.parse_args()
    pattern = args.pattern.encode()
    filename = args.file
    fast_mode = args.fast
    verbose_mode = args.verbose
    automaton = build_automaton(pattern)
    extract_matches(automaton, filename, fast_mode, verbose_mode)


if __name__ == '__main__':
    main()
