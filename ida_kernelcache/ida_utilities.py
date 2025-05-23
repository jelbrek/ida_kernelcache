#
# ida_kernelcache/ida_utilities.py
# Brandon Azad
#
# Some utility functions to make working with IDA easier.
#

from collections import deque

import os
import sys
import pathlib
import idc
import idautils
import idaapi
import ida_struct
import ida_bytes
import ida_funcs
import ida_name
import ida_auto
import ida_typeinf

read_ptr = idaapi.get_qword if idaapi.get_inf_structure().is_64bit() else idaapi.get_dword

def make_log(log_level, module):
    """Create a logging function."""
    def log(level, *args):
        if len(args) == 0:
            return level <= log.level
        if level <= log.level:
            print(module + ': ' + args[0].format(*args[1:]))
    log.level = log_level
    return log

_log = make_log(1, __name__)

WORD_SIZE = 0
"""The size of a word on the current platform."""

BIG_ENDIAN = False
"""Whether the current platform is big endian."""

LITTLE_ENDIAN = True
"""Whether the current platform is little-endian. Always the opposite of BIG_ENDIAN."""

def _initialize():
    # https://reverseengineering.stackexchange.com/questions/11396/how-to-get-the-cpu-architecture-via-idapython
    global WORD_SIZE, LITTLE_ENDIAN, BIG_ENDIAN
    info = idaapi.get_inf_structure()
    if info.is_64bit():
        WORD_SIZE = 8
    elif info.is_32bit():
        WORD_SIZE = 4
    else:
        WORD_SIZE = 2
    try:
        BIG_ENDIAN = info.is_be()
    except:
        BIG_ENDIAN = info.mf
    LITTLE_ENDIAN = not BIG_ENDIAN

_initialize()

def iterlen(iterator):
    """Consume an iterator and return its length."""
    return sum(1 for _ in iterator)

class AlignmentError(Exception):
    """An exception that is thrown if an address with improper alignment is encountered."""
    def __init__(self, address):
        self.address = address
    def __str__(self):
        return repr(self.address)

def is_mapped(ea, size=1, value=True):
    """Check if the given address is mapped.

    Specify a size greater than 1 to check if an address range is mapped.

    Arguments:
        ea: The linear address to check.

    Options:
        size: The number of bytes at ea to check. Default is 1.
        value: Only consider an address mapped if it has a value. For example, the contents of a
            bss section exist but don't have a static value. If value is False, consider such
            addresses as mapped. Default is True.

    Notes:
        This function is currently a hack: It only checks the first and last byte.
    """
    if size < 1:
        raise ValueError('Invalid argument: size={}'.format(size))
    # HACK: We only check the first and last byte, not all the bytes in between.
    if value:
        return ida_bytes.is_loaded(ea) and (size == 1 or ida_bytes.is_loaded(ea + size - 1))
    else:
        return idaapi.getseg(ea) and (size == 1 or idaapi.getseg(ea + size - 1))

def get_name_ea(name, fromaddr=idc.BADADDR):
    """Get the address of a name.

    This function returns the linear address associated with the given name.

    Arguments:
        name: The name to look up.

    Options:
        fromaddr: The referring address. Default is BADADDR. Some addresses have a
            location-specific name (for example, labels within a function). If fromaddr is not
            BADADDR, then this function will try to retrieve the address of the name from
            fromaddr's perspective. If name is not a local name, its address as a global name will
            be returned.

    Returns:
        The address of the name or BADADDR.
    """
    return idc.get_name_ea(fromaddr, name)

def get_ea_name(ea, fromaddr=idc.BADADDR, true=False, user=False):
    """Get the name of an address.

    This function returns the name associated with the byte at the specified address.

    Arguments:
        ea: The linear address whose name to find.

    Options:
        fromaddr: The referring address. Default is BADADDR. Some addresses have a
            location-specific name (for example, labels within a function). If fromaddr is not
            BADADDR, then this function will try to retrieve the name of ea from fromaddr's
            perspective. The global name will be returned if no location-specific name is found.
        true: Retrieve the true name rather than the display name. Default is False.
        user: Return "" if the name is not a user name.

    Returns:
        The name of the address or "".
    """
    if user and not idc.hasUserName(ida_bytes.get_full_flags(ea)):
        return ""
    if true:
        return ida_name.get_ea_name(fromaddr, ea)
    else:
        return idc.get_name(ea, ida_name.GN_VISIBLE | idc.calc_gtn_flags(fromaddr, ea))



def set_ea_name(ea, name, rename=False, auto=False):
    """Set the name of an address.

    Arguments:
        ea: The address to name.
        name: The new name of the address.

    Options:
        rename: If rename is False, and if the address already has a name, and if that name differs
            from the new name, then this function will fail. Set rename to True to rename the
            address even if it already has a custom name. Default is False.
        auto: If auto is True, then mark the new name as autogenerated. Default is False.

    Returns:
        True if the address was successfully named (or renamed).
    """
    if not rename and idc.hasUserName(ida_bytes.get_full_flags(ea)):
        return get_ea_name(ea) == name
    flags = idc.SN_CHECK
    if auto:
        flags |= idc.SN_AUTO
    return bool(idc.set_name(ea, name, flags))

def _insn_op_stroff_700(insn, n, sid, delta):
    """A wrapper of idc.op_stroff for IDA 7."""
    return idc.op_stroff(insn, n, sid, delta)

def _insn_op_stroff_695(insn, n, sid, delta):
    """A wrapper of idc.op_stroff for IDA 6.95."""
    return idc.op_stroff(insn.ea, n, sid, delta)

if idaapi.IDA_SDK_VERSION < 700:
    insn_op_stroff = _insn_op_stroff_695
else:
    insn_op_stroff = _insn_op_stroff_700

def _addresses(start, end, step, partial, aligned):
    """A generator to iterate over the addresses in an address range."""
    addr = start
    end_full = end - step + 1
    while addr < end_full:
        yield addr
        addr += step
    if addr != end:
        if aligned:
            raise AlignmentError(end)
        if addr < end and partial:
            yield addr

def _mapped_addresses(addresses, step, partial, allow_unmapped):
    """Wrap an _addresses generator with a filter that checks whether the addresses are mapped."""
    for addr in addresses:
        start_is_mapped = is_mapped(addr)
        end_is_mapped   = is_mapped(addr + step - 1)
        fully_mapped    = start_is_mapped and end_is_mapped
        allowed_partial = partial and (start_is_mapped or end_is_mapped)
        # Yield the value if it's sufficiently mapped. Otherwise, break if we stop at an
        # unmapped address.
        if fully_mapped or allowed_partial:
            yield addr
        elif not allow_unmapped:
            break

def Addresses(start, end=None, step=1, length=None, partial=False, aligned=False,
        unmapped=True, allow_unmapped=False):
    """A generator to iterate over the addresses in an address range.

    Arguments:
        start: The start of the address range to iterate over.

    Options:
        end: The end of the address range to iterate over.
        step: The amount to step the address by each iteration. Default is 1.
        length: The number of elements of size step to iterate over.
        partial: If only part of the element is in the address range, or if only part of the
            element is mapped, return it anyway. Default is False. This option is only meaningful
            if aligned is False or if some address in the range is partially unmapped.
        aligned: If the end address is not aligned with an iteration boundary, throw an
            AlignmentError.
        unmapped: Don't check whether an address is mapped or not before returning it. This option
            always implies allow_unmapped. Default is True.
        allow_unmapped: Don't stop iteration if an unmapped address is encountered (but the address
            won't be returned unless unmapped is also True). Default is False. If partial is also
            True, then a partially mapped address will be returned and then iteration will stop.
    """
    # HACK: We only check the first and last byte, not all the bytes in between.
    # Validate step.
    if step < 1:
        raise ValueError('Invalid arguments: step={}'.format(step))
    # Set the end address.
    if length is not None:
        end_addr = start + length * step
        if end is not None and end != end_addr:
            raise ValueError('Invalid arguments: start={}, end={}, step={}, length={}'
                    .format(start, end, step, length))
        end = end_addr
    if end is None:
        raise ValueError('Invalid arguments: end={}, length={}'.format(end, length))
    addresses = _addresses(start, end, step, partial, aligned)
    # If unmapped is True, iterate over all the addresses. Otherwise, we will check that addresses
    # are properly mapped with a wrapper.
    if unmapped:
        return addresses
    else:
        return _mapped_addresses(addresses, step, partial, allow_unmapped)

def _instructions_by_range(start, end):
    """A generator to iterate over instructions in a range."""
    pc = start
    while pc < end:
        insn = idautils.DecodeInstruction(pc)
        if insn is None:
            break
        next_pc = pc + insn.size
        """
        Sometimes IDA coalesces instruction and the lenght would take us over the end.
        One cannot know in advance in this is the case. Hence, disabling the check ...
        if next_pc > end:
            raise AlignmentError(end)
        """
        yield insn
        pc = next_pc

def _instructions_by_count(pc, count):
    """A generator to iterate over a specified number of instructions."""
    for i in range(count):
        insn = idautils.DecodeInstruction(pc)
        if insn is None:
            break
        yield insn
        pc += insn.size

def Instructions(start, end=None, count=None):
    """A generator to iterate over instructions.

    Instructions are decoded using IDA's DecodeInstruction(). If an address range is specified and
    the end of the address range does not fall on an instruction boundary, raises an
    AlignmentError.

    Arguments:
        start: The linear address from which to start decoding instructions.

    Options:
        end: The linear address at which to stop, exclusive.
        count: The number of instructions to decode.

    Notes:
        Exactly one of end and count must be specified.
    """
    if (end is not None and count is not None) or (end is None and count is None):
        raise ValueError('Invalid arguments: end={}, count={}'.format(end, count))
    if end is not None:
        return _instructions_by_range(start, end)
    else:
        return _instructions_by_count(start, count)

_FF_FLAG_FOR_SIZE = {
    1:  idc.FF_BYTE,
    2:  idc.FF_WORD,
    4:  idc.FF_DWORD,
    8:  idc.FF_QWORD,
    16: idc.FF_OWORD,
}

def word_flag(wordsize=WORD_SIZE):
    """Get the FF_xxxx flag for the given word size."""
    return _FF_FLAG_FOR_SIZE.get(wordsize, 0)

def read_word(ea, wordsize=WORD_SIZE):
    """Get the word at the given address.

    Words are read using Byte(), Word(), Dword(), or Qword(), as appropriate. Addresses are checked
    using is_mapped(). If the address isn't mapped, then None is returned.
    """
    if not is_mapped(ea, wordsize):
        return None
    if wordsize == 1:
        return idc.get_wide_byte(ea)
    if wordsize == 2:
        return idc.get_wide_word(ea)
    if wordsize == 4:
        return idc.get_wide_dword(ea)
    if wordsize == 8:
        return idc.get_qword(ea)
    raise ValueError('Invalid argument: wordsize={}'.format(wordsize))

def patch_word(ea, value, wordsize=WORD_SIZE):
    """Patch the word at the given address.

    Words are patched using PatchByte(), PatchWord(), PatchDword(), or PatchQword(), as
    appropriate.
    """
    if wordsize == 1:
        ida_bytes.patch_byte(ea, value)
    elif wordsize == 2:
        ida_bytes.patch_word(ea, value)
    elif wordsize == 4:
        ida_bytes.patch_dword(ea, value)
    elif wordsize == 8:
        ida_bytes.patch_qword(ea, value)
    else:
        raise ValueError('Invalid argument: wordsize={}'.format(wordsize))

class objectview(object):
    """A class to present an object-like view of a struct."""
    # https://goodcode.io/articles/python-dict-object/
    def __init__(self, fields, addr, size):
        self.__dict__ = fields
        self.__addr   = addr
        self.__size   = size
    def __int__(self):
        return self.__addr
    def __len__(self):
        return self.__size

def _read_struct_member_once(ea, flags, size, member_sid, member_size, asobject):
    """Read part of a struct member for _read_struct_member."""
    if ida_bytes.is_byte(flags):
        return read_word(ea, 1), 1
    elif ida_bytes.is_word(flags):
        return read_word(ea, 2), 2
    elif ida_bytes.is_dword(flags):
        return read_word(ea, 4), 4
    elif ida_bytes.is_qword(flags):
        return read_word(ea, 8), 8
    elif ida_bytes.is_oword(flags):
        return read_word(ea, 16), 16
    elif ida_bytes.is_strlit(flags):
        return idc.get_bytes(ea, size), size
    elif ida_bytes.is_float(flags):
        return idc.Float(ea), 4
    elif ida_bytes.is_double(flags):
        return idc.Double(ea), 8
    elif ida_bytes.is_struct(flags):
        value = read_struct(ea, sid=member_sid, asobject=asobject)
        return value, member_size
    return None, size

def _read_struct_member(struct, sid, union, ea, offset, name, size, asobject):
    """Read a member into a struct for read_struct."""
    flags = idc.get_member_flag(sid, offset)
    assert flags != -1
    # Extra information for parsing a struct.
    member_sid, member_ssize = None, None
    if ida_bytes.is_struct(flags):
        member_sid = idc.get_member_strid(sid, offset)
        member_ssize = ida_struct.get_struc_size(member_sid)
    # Get the address of the start of the member.
    member = ea
    if not union:
        member += offset
    # Now parse out the value.
    array = []
    processed = 0
    while processed < size:
        value, read = _read_struct_member_once(member + processed, flags, size, member_sid,
                member_ssize, asobject)
        assert size % read == 0
        array.append(value)
        processed += read
    if len(array) == 1:
        value = array[0]
    else:
        value = array
    struct[name] = value

def read_struct(ea, struct=None, sid=None, members=None, asobject=False):
    """Read a structure from the given address.

    This function reads the structure at the given address and converts it into a dictionary or
    accessor object.

    Arguments:
        ea: The linear address of the start of the structure.

    Options:
        sid: The structure ID of the structure type to read.
        struct: The name of the structure type to read.
        members: A list of the names of the member fields to read. If members is None, then all
            members are read. Default is None.
        asobject: If True, then the struct is returned as a Python object rather than a dict.

    One of sid and struct must be specified.
    """
    # Handle sid/struct.
    if struct is not None:
        sid2 = ida_struct.get_struc_id(struct)
        if sid2 == idc.BADADDR:
            raise ValueError('Invalid struc name {}'.format(struct))
        if sid is not None and sid2 != sid:
            raise ValueError('Invalid arguments: sid={}, struct={}'.format(sid, struct))
        sid = sid2
    else:
        if sid is None:
            raise ValueError('Invalid arguments: sid={}, struct={}'.format(sid, struct))
        if ida_struct.get_struc_name(sid) is None:
            raise ValueError('Invalid struc id {}'.format(sid))
    # Iterate through the members and add them to the struct.
    union = idc.is_union(sid)
    struct = {}
    for offset, name, size in idautils.StructMembers(sid):
        if members is not None and name not in members:
            continue
        _read_struct_member(struct, sid, union, ea, offset, name, size, asobject)
    if asobject:
        struct = objectview(struct, ea, ida_struct.get_struc_size(sid))
    return struct

def null_terminated(string):
    """Extract the NULL-terminated C string from the given array of bytes."""
    return string.split(b'\0', 1)[0].decode()

def _fix_unrecognized_function_insns(func):
    # Undefine every instruction that IDA does not recognize within the function
    while idc.find_func_end(func) == idc.BADADDR:
        func_properties = ida_funcs.func_t(func)
        ida_funcs.find_func_bounds(func_properties, ida_funcs.FIND_FUNC_DEFINE)
        unrecognized_insn = func_properties.end_ea
        if unrecognized_insn == 0:
            _log(1, "Could not find unrecognized instructions for function at {:#x}", func)
            return False

        # We found an unrecognized instruction, lets undefine it and explicitly make an instruction out of it!
        unrecognized_insn_end = ida_bytes.get_item_end(unrecognized_insn)
        _log(1, 'Undefining item {:#x} - {:#x}', unrecognized_insn, unrecognized_insn_end)
        ida_bytes.del_items(unrecognized_insn, ida_bytes.DELIT_EXPAND)
        if idc.create_insn(unrecognized_insn) == 0:
            _log(1, "Could not convert data at {:#x} to instruction", unrecognized_insn)
            return False
        
    return True

def _convert_address_to_function(func):
    """Convert an address that IDA has classified incorrectly into a proper function."""
    # If everything goes wrong, we'll try to restore this function.
    orig = idc.first_func_chunk(func)
    if idc.find_func_end(func) == idc.BADADDR:
        # Could not find function end, probably because IDA parsed an instruction 
        # in the middle of the function incorrectly as data. Lets try to fix the relevant insns.
        _fix_unrecognized_function_insns(func)

    else:
        # Just try removing the chunk from its current function. IDA can add it to another function
        # automatically, so make sure it's removed from all functions by doing it in loop until it
        # fails.
        for i in range(1024):
            if not idc.remove_fchunk(func, func):
                break
    # Now try making a function.
    if ida_funcs.add_func(func) != 0:
        return True
    # This is a stubborn chunk. Try recording the list of chunks, deleting the original function,
    # creating the new function, then re-creating the original function.
    if orig != idc.BADADDR:
        chunks = list(idautils.Chunks(orig))
        if ida_funcs.del_func(orig) != 0:
            # Ok, now let's create the new function, and recreate the original.
            if ida_funcs.add_func(func) != 0:
                if ida_funcs.add_func(orig) != 0:
                    # Ok, so we created the functions! Now, if any of the original chunks are not
                    # contained in a function, we'll abort and undo.
                    if all(idaapi.get_func(start) for start, end in chunks):
                        return True
            # Try to undo the damage.
            for start, _ in chunks:
                ida_funcs.del_func(start)
    # Everything we've tried so far has failed. If there was originally a function, try to restore
    # it.
    if orig != idc.BADADDR:
        _log(0, 'Trying to restore original function {:#x}', orig)
        ida_funcs.add_func(orig)
    return False

def is_function_start(ea):
    """Return True if the address is the start of a function."""
    return idc.get_func_attr(ea, idc.FUNCATTR_START) == ea

def force_function(addr):
    """Ensure that the given address is a function type, converting it if necessary."""
    if is_function_start(addr):
        return True
    return _convert_address_to_function(addr)

def ReadWords(start, end, step=WORD_SIZE, wordsize=WORD_SIZE, addresses=False):
    """A generator to iterate over the data words in the given address range.

    The iterator returns a stream of words or tuples for each mapped word in the address range.
    Words are read using read_word(). Iteration stops at the first unmapped word.

    Arguments:
        start: The start address.
        end: The end address.

    Options:
        step: The number of bytes to advance per iteration. Default is WORD_SIZE.
        wordsize: The word size to read, in bytes. Default is WORD_SIZE.
        addresses: If true, then the iterator will return a stream of tuples (word, ea) for each
            mapped word in the address range. Otherwise, just the word itself will be returned.
            Default is False.
    """
    for addr in Addresses(start, end, step=step, unmapped=True):
        word = read_word(addr, wordsize)
        if word is None:
            break
        value = (word, addr) if addresses else word
        yield value

def WindowWords(start, end, window_size, wordsize=WORD_SIZE):
    """A generator to iterate over a sliding window of data words in the given address range.

    The iterator returns a stream of tuples (window, ea) for each word in the address range. The
    window is a deque of the window_size words at address ea. The deque is owned by the generator
    and its contents will change between iterations.
    """
    words = ReadWords(start, end, wordsize=wordsize)
    window = deque([next(words) for _ in range(window_size)], maxlen=window_size)
    addr = start
    yield window, addr
    for word in words:
        window.append(word)
        addr += wordsize
        yield window, addr

def struct_create(name, union=False):
    """Create an IDA struct with the given name, returning the SID."""
    # AddStrucEx is documented as returning -1 on failure, but in practice it seems to return
    # BADADDR.
    til = ida_typeinf.get_idati()
    ordinal = ida_typeinf.get_type_ordinal(til, name)
    if ordinal in (0, -1, idc.BADADDR, ida_typeinf.BADORD):
        union = 1 if union else 0
        sid = idc.add_struc(-1, name, union)
    else:
        tif = ida_typeinf.tinfo_t()
        tif.get_numbered_type(til, ordinal, ida_typeinf.BTF_UNION if union else ida_typeinf.BTF_STRUCT)
        if tif.is_forward_decl():
            # create a new struct/union tif:
            udt = ida_typeinf.udt_type_data_t()
            udt.name = name
            udt.is_union = union
            tif = ida_typeinf.tinfo_t()
            tif.create_udt(udt)
            # replace old ordinal with new struct:
            tif.set_numbered_type(til, ordinal, ida_typeinf.NTF_REPLACE, name)
            sid = ida_struct.get_struc_id(name)
        else:
            # type exists but is not a forward decl?
            return None
    if sid in (-1, idc.BADADDR):
        return None
    return sid

def struct_open(name, create=False, union=None):
    """Get the SID of the IDA struct with the given name, optionally creating it."""
    sid = ida_struct.get_struc_id(name)
    if sid == idc.BADADDR:
        if not create:
            return None
        sid = struct_create(name, union=bool(union))
    elif union is not None:
        is_union = bool(idc.is_union(sid))
        if union != is_union:
            return None
    return sid

def struct_member_offset(sid, name):
    """A version of IDA's GetMemberOffset() that also works with unions."""
    struct = idaapi.get_struc(sid)
    if not struct:
        return None
    member = idaapi.get_member_by_name(struct, name)
    if not member:
        return None
    return member.soff

def struct_add_word(sid, name, offset, size, count=1):
    """Add a word (integer) to a structure.

    If sid is a union, offset must be -1.
    """
    return idc.add_struc_member(sid, name, offset, idc.FF_DATA | word_flag(size), -1, size * count)

def struct_add_ptr(sid, name, offset, count=1, type=None):
    """Add a pointer to a structure.

    If sid is a union, offset must be -1.
    """
    ptr_flag = idc.FF_DATA | word_flag(WORD_SIZE) | ida_bytes.off_flag()
    ret = idc.add_struc_member(sid, name, offset, ptr_flag, 0, WORD_SIZE)
    if ret == 0 and type is not None:
        if offset == -1:
            offset = struct_member_offset(sid, name)
            assert offset is not None
        mid = idc.get_member_id(sid, offset)
        idc.SetType(mid, type)
    return ret

def struct_add_struct(sid, name, offset, msid, count=1):
    """Add a structure member to a structure.

    If sid is a union, offset must be -1.
    """
    size = ida_struct.get_struc_size(msid)
    return idc.add_struc_member(sid, name, offset, idc.FF_DATA | ida_bytes.FF_STRUCT, msid, size * count)

def remove_typelibs():
    """Iterate over all .til files amd attempt to remove them
    """
    til_dir = pathlib.Path(sys.executable).parent / "til"
    for root, dirs, files in os.walk(til_dir):
        for file in files:
            if file.endswith(".til"):
                til = os.path.splitext(file)[0]
                ida_typeinf.del_til(til)
    
