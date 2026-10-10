"""Reading and writing the binary .mesh and .anim formats. Author: ross-g."""

import json
import logging
import mmap
import xml.etree.ElementTree as Xml
from struct import pack, unpack_from


DATA_LOG = logging.getLogger("io_pdx.data")


class PDXData(object):
    def __init__(self, element, depth=None):
        self.name = element.tag

        self.depth = depth or 0

        self.attrlist = []

        for attr in element.attrib:
            setattr(self, attr, element.attrib[attr])
            self.attrlist.append(attr)

        for child in list(element):
            child_data = type(self)(child, self.depth + 1)
            if hasattr(self, child.tag):
                curr_data = getattr(self, child.tag)
                if isinstance(curr_data, list):
                    curr_data.append(child_data)
                else:
                    setattr(self, child.tag, [curr_data, child_data])
            else:
                setattr(self, child.tag, child_data)
                self.attrlist.append(child.tag)

    def __str__(self):
        indent = " " * 4
        string = []

        for _key in self.attrlist:
            _val = getattr(self, _key)

            if isinstance(_val, type(self)):
                string.append(f"{self.depth * indent}{_key}:")
                string.append(f"{_val}")

            else:
                if all(isinstance(v, type(self)) for v in _val):
                    for v in _val:
                        string.append(f"{self.depth * indent}{_key}:")
                        string.append(f"{v}")
                else:
                    data_len = len(_val)
                    data_type = list(set(type(v) for v in _val))[0].__name__
                    string.append(f"{self.depth * indent}{_key} ({data_type}, {data_len}):  {_val}")

        return "\n".join(string)


class PDXDataJSON(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, PDXData):
            d = {}
            for attr in obj.attrlist:
                val = getattr(obj, attr)
                if isinstance(val, list):
                    d[attr] = [v for v in val]
                else:
                    d[attr] = val
            return d
        return super().default(obj)


def parseObject(bdata, pos):
    objdepth = 0
    while unpack_from("c", bdata, offset=pos)[0].decode() == "[":
        objdepth += 1
        pos += 1

    obj_name = ""
    while unpack_from("b", bdata, offset=pos)[0] != 0:
        obj_name += unpack_from("c", bdata, offset=pos)[0].decode("latin-1")
        pos += 1

    pos += 1

    return obj_name, objdepth, pos


def parseProperty(bdata, pos):
    pos += 1

    prop_name_length = unpack_from("b", bdata, offset=pos)[0]
    pos += 1

    prop_name = parseString(bdata, pos, prop_name_length)
    pos += prop_name_length

    prop_values, pos = parseData(bdata, pos)

    return prop_name, prop_values, pos


def parseString(bdata, pos, length):
    val_tuple = unpack_from("c" * length, bdata, offset=pos)

    string = b"".join(val_tuple).decode("latin-1")

    if string[-1] == chr(0):
        string = string[:-1]

    return string


def parseData(bdata, pos):
    datatype = unpack_from("c", bdata, offset=pos)[0].decode()
    pos += 1
    datacount = unpack_from("i", bdata, offset=pos)[0]
    pos += 4
    datavalues = []

    if datatype == "i":
        val = unpack_from("i" * datacount, bdata, offset=pos)
        datavalues.extend(val)
        pos += 4 * datacount

    elif datatype == "f":
        val = unpack_from("f" * datacount, bdata, offset=pos)
        datavalues.extend(val)
        pos += 4 * datacount

    elif datatype == "s":
        str_data_length = unpack_from("i", bdata, offset=pos)[0]
        pos += 4

        val = parseString(bdata, pos, str_data_length)
        datavalues.append(val)
        pos += str_data_length

    else:
        raise NotImplementedError(
            f"Unknown data type encountered. {datatype} at position {pos} eg: {bdata[pos - 10 : pos + 10]}"
        )

    return datavalues, pos


def read_meshfile(filepath):
    with open(filepath, "rb") as fp:
        with mmap.mmap(fp.fileno(), length=0, access=mmap.ACCESS_READ) as mm_fp:
            fdata = mm_fp.read(mm_fp.size())

    file_element = Xml.Element("File")

    eof = len(fdata)
    pos = 0

    header = unpack_from("4c", fdata, pos)
    if bytes(b"".join(header)) == b"@@b@":
        pos += 4
    else:
        raise NotImplementedError(f"Unknown file header: {header}")

    parent_element = file_element
    depth_list = [file_element]
    current_depth = 0

    while pos < eof:
        next_char = unpack_from("c", fdata, offset=pos)[0].decode()
        if next_char == "[":
            obj_name, depth, pos = parseObject(fdata, pos)

            if not depth > current_depth:
                depth_list = depth_list[:depth]
                parent_element = depth_list[-1]

            new_element = Xml.SubElement(parent_element, obj_name)
            parent_element = new_element
            depth_list.append(parent_element)
            current_depth = depth

        elif next_char == "!":
            prop_name, prop_values, pos = parseProperty(fdata, pos)

            parent_element.set(prop_name, prop_values)

        else:
            raise NotImplementedError("Unknown object encountered.")

    return file_element


def writeObject(obj_xml, obj_depth):
    DATA_LOG.debug("writeObject: %s", obj_depth * "-")
    datastring = b""

    for _ in range(obj_depth):
        datastring += pack("c", "[".encode())

    obj_name = obj_xml.tag
    if not len(obj_name) < 64:
        raise NotImplementedError(f"Object name is longer than 64 characters: {obj_name}")
    datastring += writeString(obj_name)
    datastring += pack("x")

    return datastring


def writeProperty(prop_name, prop_data):
    DATA_LOG.debug("writeProperty:")
    datastring = b""

    try:
        datastring += pack("c", "!".encode())

        prop_name_length = len(prop_name)
        datastring += pack("b", prop_name_length)

        datastring += writeString(prop_name)

        datastring += writeData(prop_data)

    except NotImplementedError as err:
        DATA_LOG.error(f"Failed writing property: {prop_name}")
        raise err

    return datastring


def writeString(string):
    DATA_LOG.debug("writeString: '%s'", string)
    datastring = b""

    try:
        string = string.encode("latin-1")
    except UnicodeEncodeError as err:
        DATA_LOG.error(f"String '{string}' contains characters outside the Latin-1 set.")
        raise err

    datastring += pack(f"{len(string)}s", string)

    return datastring


def writeData(data_array):
    DATA_LOG.debug("writeData: [%s]", ", ".join([f"{d}" for d in data_array]))
    datastring = b""

    types = set([type(d) for d in data_array])
    if len(types) == 1:
        datatype = types.pop()
    elif len(types) < 1:
        return datastring
    else:
        raise NotImplementedError(f"Mixed data types encountered: {types}")

    if all(isinstance(d, int) for d in data_array):
        datastring += pack("c", "i".encode())

        size = len(data_array)
        datastring += pack("i", size)

        datastring += pack("i" * size, *data_array)

    elif all(isinstance(d, float) for d in data_array):
        datastring += pack("c", "f".encode())

        size = len(data_array)
        datastring += pack("i", size)

        datastring += pack("f" * size, *data_array)

    elif all(isinstance(d, str) for d in data_array):
        datastring += pack("c", "s".encode())

        size = 1
        datastring += pack("i", size)

        str_data_length = len(data_array[0])
        datastring += pack("i", (str_data_length + 1))

        datastring += writeString(data_array[0])
        datastring += pack("x")

    else:
        raise NotImplementedError(f"Unknown data type encountered. {datatype} eg: {data_array[0]}")

    return datastring


def write_meshfile(filepath, root_xml):
    datastring = b""

    header = "@@b@"
    for x in header:
        datastring += pack("c", x.encode())

    if root_xml.tag == "File":
        datastring += writeProperty("pdxasset", root_xml.get("pdxasset"))
    else:
        raise NotImplementedError(f"Unknown XML root encountered. {root_xml.tag}")

    object_xml = root_xml.find("object")
    if object_xml is not None:
        current_depth = 1
        datastring += writeObject(object_xml, current_depth)

        for shape_xml in object_xml:
            current_depth = 2
            datastring += writeObject(shape_xml, current_depth)

            for prop in ["lod"]:
                if shape_xml.get(prop) is not None:
                    datastring += writeProperty(prop, shape_xml.get(prop))

            for child_xml in shape_xml:
                current_depth = 3
                datastring += writeObject(child_xml, current_depth)

                if child_xml.tag == "mesh":
                    mesh_xml = child_xml
                    for prop in ["p", "n", "ta", "u0", "u1", "u2", "u3", "tri", "boundingsphere"]:
                        if mesh_xml.get(prop) is not None:
                            datastring += writeProperty(prop, mesh_xml.get(prop))

                    aabb_xml = mesh_xml.find("aabb")
                    if aabb_xml is not None:
                        current_depth = 4
                        datastring += writeObject(aabb_xml, current_depth)
                        for prop in ["min", "max"]:
                            if aabb_xml.get(prop) is not None:
                                datastring += writeProperty(prop, aabb_xml.get(prop))

                    material_xml = mesh_xml.find("material")
                    if material_xml is not None:
                        current_depth = 4
                        datastring += writeObject(material_xml, current_depth)
                        for prop in ["shader", "diff", "n", "spec"]:
                            if material_xml.get(prop) is not None:
                                datastring += writeProperty(prop, material_xml.get(prop))

                    skin_xml = mesh_xml.find("skin")
                    if skin_xml is not None:
                        current_depth = 4
                        datastring += writeObject(skin_xml, current_depth)
                        for prop in ["bones", "ix", "w"]:
                            if skin_xml.get(prop) is not None:
                                datastring += writeProperty(prop, skin_xml.get(prop))

                elif child_xml.tag == "skeleton":
                    for bone_xml in child_xml:
                        current_depth = 4
                        datastring += writeObject(bone_xml, current_depth)
                        for prop in ["ix", "pa", "tx"]:
                            if bone_xml.get(prop) is not None:
                                datastring += writeProperty(prop, bone_xml.get(prop))

    locator_xml = root_xml.find("locator")
    if locator_xml is not None:
        current_depth = 1
        datastring += writeObject(locator_xml, current_depth)

        for locnode_xml in locator_xml:
            current_depth = 2
            datastring += writeObject(locnode_xml, current_depth)

            for prop in ["p", "q", "pa", "tx"]:
                if locnode_xml.get(prop) is not None:
                    datastring += writeProperty(prop, locnode_xml.get(prop))

    with open(filepath, "wb") as fp:
        fp.write(datastring)


def write_animfile(filepath, root_xml):
    datastring = b""

    header = "@@b@"
    for x in header:
        datastring += pack("c", x.encode())

    if root_xml.tag == "File":
        datastring += writeProperty("pdxasset", root_xml.get("pdxasset"))
    else:
        raise NotImplementedError(f"Unknown XML root encountered. {root_xml.tag}")

    info_xml = root_xml.find("info")
    if info_xml is not None:
        current_depth = 1
        datastring += writeObject(info_xml, current_depth)

        for prop in ["fps", "sa", "j"]:
            if info_xml.get(prop) is not None:
                datastring += writeProperty(prop, info_xml.get(prop))

        for bone_xml in info_xml:
            current_depth = 2
            datastring += writeObject(bone_xml, current_depth)

            for prop in ["sa", "t", "q", "s"]:
                if bone_xml.get(prop) is not None:
                    datastring += writeProperty(prop, bone_xml.get(prop))

    samples_xml = root_xml.find("samples")
    if samples_xml is not None:
        current_depth = 1
        datastring += writeObject(samples_xml, current_depth)

        for prop in ["t", "q", "s"]:
            if samples_xml.get(prop) is not None:
                datastring += writeProperty(prop, samples_xml.get(prop))

    with open(filepath, "wb") as fp:
        fp.write(datastring)


