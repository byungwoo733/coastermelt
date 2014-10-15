#!/usr/bin/env python

# Tools for working with code on the backdoored target machine.
# Assemble, disassemble, and compile. Requires an ARM cross-compiler.

CC      = 'arm-none-eabi-gcc'
OBJCOPY = 'arm-none-eabi-objcopy'
OBJDUMP = 'arm-none-eabi-objdump'

__all__ = [
    'disassemble_string', 'disassemble',
    'assemble', 'compile', 'evalc',
    'pad', 'defines', 'includes',
]

import remote, os, random, struct
from subprocess import check_call, check_output
from dump import read_block

# Global scratchpad memory. This is the address of the biggest safest area of
# read-write-execute RAM we can guesstimate about. This is provided as a
# default location for the backdoor commands here to hastily bludgeon data
# into. How did we find it? Guesswork! Also, staring at memsquares!
#
# This is halfway through DRAM, in a spot that seems to exhibit uninitialized
# memory patterns even after quite a lot of time running memsquare.py
# Hopefully our code won't get trashed by a SCSI packet!

pad = 0x1e00000

# Default global defines for C++ and assembly code we compile

defines = {
    'pad': pad
}

# Default list of C++ snippets.

includes = []


def tempnames(*suffixes):
    # Return temporary file names that are good enough for our purposes
    base = 'temp-coastermelt-' + str(random.randint(100000, 999999))
    return [base + s for s in suffixes]

def cleanup(filenames, leave_temp_files = False):
    if not leave_temp_files:
        for f in filenames:
            try:
                os.remove(f)
            except OSError:
                pass

def write_file(file, data):
    f = open(file, 'w')
    f.write(data)
    f.close()

def read_file(file):
    f = open(file, 'rb')
    d = f.read()
    f.close()
    return d


def disassemble_string(data, address = 0, thumb = True, leave_temp_files = False):
    """Disassemble code from a string buffer.
       Returns a string made of up multiple lines, each with
       the address in hex, a tab, then the disassembled code.
       """
    bin, = temps = tempnames('.bin')
    try:
        write_file(bin, data)

        text = check_output([
            OBJDUMP, '-D', '-w', '-z',
            '-b', 'binary', '-m', 'arm7tdmi', 
            '--prefix-addresses',
            '--adjust-vma', '0x%08x' % address,
            '-M', ('force-thumb', 'no-force-thumb')[not thumb],
            bin])

        lines = text.split('\n')
        lines = ['%s\t%s' % (l[2:10], l[11:]) for l in lines if l.startswith('0x')]
        return '\n'.join(lines)

    finally:
        cleanup(temps, leave_temp_files)

def disassemble(d, address, size, thumb = True, leave_temp_files = False):
    """Read some bytes of ARM memory and try to disassemble it as code.
       Returns a string made of up multiple lines, each with the address
       in hex, a tab, then the disassembled code.
       """
    return disassemble_string(read_block(d, address, size), address, thumb, leave_temp_files)

def assemble(d, address, text, defines = defines, leave_temp_files = False):
    """Assemble some instructions for the ARM and place them in memory.
       Address must be word aligned.
       """
    if address & 3:
        raise ValueError("Address needs to be word aligned")

    src, obj, bin, ld = temps = tempnames('.s', '.o', '.bin', '.ld')
    try:

        # Linker script
        write_file(ld, '''

            MEMORY {
                PATCH (rx) : ORIGIN = 0x%08x, LENGTH = 2048K
            }

            SECTIONS {
                .text : {
                    *(.text)
                } > PATCH
            }

            ''' % address)

        # Assembly source
        write_file(src, '''

            .text
            .syntax unified
            .thumb
            %s
            .global _start 
        _start:
            %s
            .align  2
            .pool
            .align  2

            ''' % (
                '\n'.join(['.equ %s, 0x%08x' % i for i in defines.items()]),
                text
            )
        )

        check_call([ CC, '-nostdlib', '-nostdinc', '-o', obj, src, '-T', ld ])
        check_call([ OBJCOPY, obj, '-O', 'binary', bin ])
        data = read_file(bin)
        words = struct.unpack('<%dI' % (len(data)/4), data)

    finally:
        cleanup(temps, leave_temp_files)

    for i, word in enumerate(words):
        d.poke(address + 4*i, word)

def compile(d, address, expression, includes = includes, defines = defines,
    show_disassembly = False, thumb = True, leave_temp_files = False):
    """Compile a C++ expression to a stand-alone patch installed starting at the supplied address.

       The 'includes' list contains text of C++ definitions and declarations that
       go above the function containing our expression. The 'globals' dictionary
       can define uint32_t constants that are available even prior to the includes.
       """

    src, obj, bin, ld = temps = tempnames('.cpp', '.o', '.bin', '.ld')
    try:

        whole_src = (
            '#include <stdint.h>\n'
            '%s\n'
            '%s\n'
            'uint32_t __attribute__ ((externally_visible, section(".first"))) start(unsigned arg) {\n'
            '  return ( %s );\n'
            '}'
        ) % (
            '\n'.join(['static const uint32_t %s = 0x%08x;' % i for i in defines.items()]),
            '\n'.join(includes),
            expression
        )

        # Linker script
        write_file(ld, '''

            MEMORY {
                PATCH (rx) : ORIGIN = 0x%08x, LENGTH = 2048K
            }

            SECTIONS {
                .text : {
                    *(.first) *(.text) *(.rodata)
                } > PATCH
            }

            ''' % address)

        write_file(src, whole_src)
        check_call([ CC, '-nostdlib',
            '-o', obj, src, '-T', ld,
            '-Os', '-fwhole-program',
            ('-mthumb', '-mno-thumb')[not thumb]])
        
        check_call([ OBJCOPY, obj, '-O', 'binary', bin ])
        data = read_file(bin)

        if show_disassembly:
            print "========= C++ source ========= [%08x]" % address
            print whole_src
            print "========= Disassembly ========"
            print disassemble_string(data, address, thumb)
            print "=============================="

        words = struct.unpack('<%dI' % (len(data)/4), data)

    finally:
        cleanup(temps, leave_temp_files)

    for i, word in enumerate(words):
        d.poke(address + 4*i, word)


def evalc(d, expression, arg = 0, includes = includes, defines = defines, address = pad, show_disassembly = False):
    """Compile and remotely execute a C++ expression"""
    compile(d, address, expression, includes=includes, defines=defines, show_disassembly=show_disassembly)
    return d.blx(address + 1, arg)[0]


if __name__ == '__main__':
    # Example

    d = remote.Device()

    print disassemble(d, pad, 0x20)

    assemble(d, pad, 'nop\n' * 100)

    assemble(d, pad, '''
        nop
        bl      0x1fffd00
        ldr     r0, =0x1234abcd
        blx     r0
        bx      lr
        ''')

    print
    print disassemble(d, pad, 0x20)

    lib = '''
        int multiply(int a, int b) {
            return a * b;
        }
        '''

    # C++ one-liners
    compile(d, pad, 'multiply(arg, 5)', includes = [lib], show_disassembly = True)

    # Test the C++ function
    for n in 1, 2, 3, 500, 0:
        assert d.blx(pad+1, n)[0] == n * 5

    # An even higher level C++ example
    assert 5 == evalc(d, '5')
    assert 10 == evalc(d, 'arg', 10)
    assert 0xabc0000 ==  evalc(d, 'funk << 16', defines={'funk':0xabc})

    print "\nSuccessfully called compiled C++ code on the target!"