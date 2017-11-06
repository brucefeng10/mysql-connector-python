# MySQL Connector/Python - MySQL driver written in Python.
# Copyright (c) 2016, 2017, Oracle and/or its affiliates. All rights reserved.

# MySQL Connector/Python is licensed under the terms of the GPLv2
# <http://www.gnu.org/licenses/old-licenses/gpl-2.0.html>, like most
# MySQL Connectors. There are special exceptions to the terms and
# conditions of the GPLv2 as it is applied to this software, see the
# FOSS License Exception
# <http://www.mysql.com/about/legal/licensing/foss-exception.html>.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301 USA

"""Implementation of the X protocol for MySQL servers."""

import struct

from .compat import STRING_TYPES, INT_TYPES
from .errors import InterfaceError, OperationalError, ProgrammingError
from .expr import (ExprParser, build_expr, build_scalar, build_bool_scalar,
                   build_int_scalar)
from .helpers import encode_to_bytes, get_item_or_attr
from .result import ColumnMetaData
from .protobuf import SERVER_MESSAGES, Message, mysqlxpb_enum


class MessageReaderWriter(object):
    """Implements a Message Reader/Writer.

    Args:
        socket_stream (mysqlx.connection.SocketStream): `SocketStream` object.
    """
    def __init__(self, socket_stream):
        self._stream = socket_stream
        self._msg = None

    def _read_message(self):
        """Read message.

        Raises:
            :class:`mysqlx.ProgrammingError`: If e connected server does not
                                              have the MySQL X protocol plugin
                                              enabled.

        Returns:
            mysqlx.protobuf.Message: MySQL X Protobuf Message.
        """
        hdr = self._stream.read(5)
        msg_len, msg_type = struct.unpack("<LB", hdr)
        if msg_type == 10:
            raise ProgrammingError("The connected server does not have the "
                                   "MySQL X protocol plugin enabled")
        payload = self._stream.read(msg_len - 1)
        msg_type_name = SERVER_MESSAGES.get(msg_type)
        if not msg_type_name:
            raise ValueError("Unknown msg_type: {0}".format(msg_type))
        msg = Message.from_server_message(msg_type, payload)
        return msg

    def read_message(self):
        """Read message.

        Returns:
            mysqlx.protobuf.Message: MySQL X Protobuf Message.
        """
        if self._msg is not None:
            msg = self._msg
            self._msg = None
            return msg
        return self._read_message()

    def push_message(self, msg):
        """Push message.

        Args:
            msg (mysqlx.protobuf.Message): MySQL X Protobuf Message.

        Raises:
            :class:`mysqlx.OperationalError`: If message push slot is full.
        """
        if self._msg is not None:
            raise OperationalError("Message push slot is full")
        self._msg = msg

    def write_message(self, msg_id, msg):
        """Write message.

        Args:
            msg_id (int): The message ID.
            msg (mysqlx.protobuf.Message): MySQL X Protobuf Message.
        """
        msg_str = encode_to_bytes(msg.serialize_to_string())
        header = struct.pack("<LB", len(msg_str) + 1, msg_id)
        self._stream.sendall(b"".join([header, msg_str]))


class Protocol(object):
    """Implements the MySQL X Protocol.

    Args:
        read_writer (mysqlx.protocol.MessageReaderWriter): A Message \
            Reader/Writer object.
    """
    def __init__(self, reader_writer):
        self._reader = reader_writer
        self._writer = reader_writer
        self._message = None

    def _apply_filter(self, msg, stmt):
        """Apply filter.

        Args:
            msg (mysqlx.protobuf.Message): The MySQL X Protobuf Message.
            stmt (Statement): A `Statement` based type object.
        """
        if stmt.has_where:
            msg["criteria"] = stmt.get_where_expr()
        if stmt.has_bindings:
            msg["args"].extend(self.get_binding_scalars(stmt))
        if stmt.has_limit:
            msg["limit"] = Message(
                "Mysqlx.Crud.Limit",
                row_count=stmt.get_limit_row_count(),
                offset=stmt.get_limit_offset())
        if stmt.has_sort:
            msg["order"].extend(stmt.get_sort_expr())
        if stmt.has_group_by:
            msg["grouping"].extend(stmt.get_grouping())
        if stmt.has_having:
            msg["grouping_criteria"] = stmt.get_having()

    def _create_any(self, arg):
        """Create any.

        Args:
            arg (object): Arbitrary object.

        Returns:
            mysqlx.protobuf.Message: MySQL X Protobuf Message.
        """
        if isinstance(arg, STRING_TYPES):
            value = Message("Mysqlx.Datatypes.Scalar.String", value=arg)
            scalar = Message("Mysqlx.Datatypes.Scalar", type=8, v_string=value)
            return Message("Mysqlx.Datatypes.Any", type=1, scalar=scalar)
        elif isinstance(arg, bool):
            return Message("Mysqlx.Datatypes.Any", type=1,
                           scalar=build_bool_scalar(arg))
        elif isinstance(arg, INT_TYPES):
            return Message("Mysqlx.Datatypes.Any", type=1,
                           scalar=build_int_scalar(arg))
        return None

    def _process_frame(self, msg, result):
        """Process frame.

        Args:
            msg (mysqlx.protobuf.Message): A MySQL X Protobuf Message.
            result (Result): A `Result` based type object.
        """
        if msg["type"] == 1:
            warn_msg = Message.from_message("Mysqlx.Notice.Warning",
                                            msg["payload"])
            result.append_warning(warn_msg.level, warn_msg.code, warn_msg.msg)
        elif msg["type"] == 2:
            Message.from_message("Mysqlx.Notice.SessionVariableChanged",
                                 msg["payload"])
        elif msg["type"] == 3:
            sess_state_msg = Message.from_message(
                "Mysqlx.Notice.SessionStateChanged", msg["payload"])
            if sess_state_msg["param"] == mysqlxpb_enum(
                    "Mysqlx.Notice.SessionStateChanged.Parameter."
                    "ROWS_AFFECTED"):
                result.set_rows_affected(get_item_or_attr(
                    sess_state_msg["value"], "v_unsigned_int"))
            elif sess_state_msg["param"] == mysqlxpb_enum(
                    "Mysqlx.Notice.SessionStateChanged.Parameter."
                    "GENERATED_INSERT_ID"):
                result.set_generated_id(get_item_or_attr(
                    sess_state_msg["value"], "v_unsigned_int"))

    def _read_message(self, result):
        """Read message.

        Args:
            result (Result): A `Result` based type object.
        """
        while True:
            msg = self._reader.read_message()
            if msg.type == "Mysqlx.Error":
                raise OperationalError(msg["msg"])
            elif msg.type == "Mysqlx.Notice.Frame":
                self._process_frame(msg, result)
            elif msg.type == "Mysqlx.Sql.StmtExecuteOk":
                return None
            elif msg.type == "Mysqlx.Resultset.FetchDone":
                result.set_closed(True)
            elif msg.type == "Mysqlx.Resultset.FetchDoneMoreResultsets":
                result.set_has_more_results(True)
            else:
                break
        return msg

    def get_capabilites(self):
        """Get capabilities.

        Returns:
            mysqlx.protobuf.Message: MySQL X Protobuf Message.
        """
        msg = Message("Mysqlx.Connection.CapabilitiesGet")
        self._writer.write_message(
            mysqlxpb_enum("Mysqlx.ClientMessages.Type.CON_CAPABILITIES_GET"),
            msg)
        return self._reader.read_message()

    def set_capabilities(self, **kwargs):
        """Set capabilities.

        Args:
            **kwargs: Arbitrary keyword arguments.

        Returns:
            mysqlx.protobuf.Message: MySQL X Protobuf Message.
        """
        capabilities = Message("Mysqlx.Connection.Capabilities")
        for key, value in kwargs.items():
            capability = Message("Mysqlx.Connection.Capability")
            capability["name"] = key
            capability["value"] = self._create_any(value)
            capabilities["capabilities"].extend([capability.get_message()])
        msg = Message("Mysqlx.Connection.CapabilitiesSet")
        msg["capabilities"] = capabilities
        self._writer.write_message(
            mysqlxpb_enum("Mysqlx.ClientMessages.Type.CON_CAPABILITIES_SET"),
            msg)
        return self.read_ok()

    def send_auth_start(self, method, auth_data=None, initial_response=None):
        """Send authenticate start.

        Args:
            method (str): Message method.
            auth_data (Optional[str]): Authentication data.
            initial_response (Optional[str]): Initial response.
        """
        msg = Message("Mysqlx.Session.AuthenticateStart")
        msg["mech_name"] = method
        if auth_data is not None:
            msg["auth_data"] = auth_data
        if initial_response is not None:
            msg["initial_response"] = initial_response
        self._writer.write_message(mysqlxpb_enum(
            "Mysqlx.ClientMessages.Type.SESS_AUTHENTICATE_START"), msg)

    def read_auth_continue(self):
        """Read authenticate continue.

        Raises:
            :class:`InterfaceError`: If the message type is not
                                     `Mysqlx.Session.AuthenticateContinue`

        Returns:
            str: The authentication data.
        """
        msg = self._reader.read_message()
        if msg.type != "Mysqlx.Session.AuthenticateContinue":
            raise InterfaceError("Unexpected message encountered during "
                                 "authentication handshake")
        return msg["auth_data"]

    def send_auth_continue(self, data):
        """Send authenticate continue.

        Args:
            data (str): Authentication data.
        """
        msg = Message("Mysqlx.Session.AuthenticateContinue",
                      auth_data=data)
        self._writer.write_message(mysqlxpb_enum(
            "Mysqlx.ClientMessages.Type.SESS_AUTHENTICATE_CONTINUE"), msg)

    def read_auth_ok(self):
        """Read authenticate OK.

        Raises:
            :class:`mysqlx.InterfaceError`: If message type is `Mysqlx.Error`.
        """
        while True:
            msg = self._reader.read_message()
            if msg.type == "Mysqlx.Session.AuthenticateOk":
                break
            if msg.type == "Mysqlx.Error":
                raise InterfaceError(msg.msg)

    def get_binding_scalars(self, stmt):
        """Returns the binding scalars.

        Raises:
            :class:`mysqlx.ProgrammingError`: If unable to find placeholder for
                                              parameter.

        Returns:
            list: A list of ``mysqlx.protobuf.Message`` objects.
        """
        count = len(stmt.get_binding_map())
        scalars = count * [None]
        binding_map = stmt.get_binding_map()
        for binding in stmt.get_bindings():
            name = binding["name"]
            if name not in binding_map:
                raise ProgrammingError("Unable to find placeholder for "
                                       "parameter: {0}".format(name))
            pos = binding_map[name]
            scalars[pos] = build_scalar(binding["value"]).get_message()
        return scalars

    def send_find(self, stmt):
        """Send find.

        Args:
            stmt (Statement): A :class:`mysqlx.ReadStatement` or
                              :class:`mysqlx.FindStatement` object.
        """
        data_model = mysqlxpb_enum("Mysqlx.Crud.DataModel.DOCUMENT"
                                   if stmt.is_doc_based() else
                                   "Mysqlx.Crud.DataModel.TABLE")
        collection = Message("Mysqlx.Crud.Collection",
                             name=stmt.target.name,
                             schema=stmt.schema.name)
        msg = Message("Mysqlx.Crud.Find", data_model=data_model,
                      collection=collection)
        if stmt.has_projection:
            msg["projection"] = stmt.get_projection_expr()
        self._apply_filter(msg, stmt)

        if stmt.is_lock_exclusive():
            msg["locking"] = \
                mysqlxpb_enum("Mysqlx.Crud.Find.RowLock.EXCLUSIVE_LOCK")
        elif stmt.is_lock_shared():
            msg["locking"] = \
                mysqlxpb_enum("Mysqlx.Crud.Find.RowLock.SHARED_LOCK")

        self._writer.write_message(
            mysqlxpb_enum("Mysqlx.ClientMessages.Type.CRUD_FIND"), msg)

    def send_update(self, stmt):
        """Send update.

        Args:
            stmt (Statement): A :class:`mysqlx.ModifyStatement` or
                              :class:`mysqlx.UpdateStatement` object.
        """
        data_model = mysqlxpb_enum("Mysqlx.Crud.DataModel.DOCUMENT"
                                   if stmt.is_doc_based() else
                                   "Mysqlx.Crud.DataModel.TABLE")
        collection = Message("Mysqlx.Crud.Collection",
                             name=stmt.target.name,
                             schema=stmt.schema.name)
        msg = Message("Mysqlx.Crud.Update", data_model=data_model,
                      collection=collection)
        self._apply_filter(msg, stmt)
        for update_op in stmt.get_update_ops():
            operation = Message("Mysqlx.Crud.UpdateOperation")
            operation["operation"] = update_op.update_type
            operation["source"] = update_op.source
            if update_op.value is not None:
                operation["value"] = build_expr(update_op.value)
            msg["operation"].extend([operation.get_message()])

        self._writer.write_message(
            mysqlxpb_enum("Mysqlx.ClientMessages.Type.CRUD_UPDATE"), msg)

    def send_delete(self, stmt):
        """Send delete.

        Args:
            stmt (Statement): A :class:`mysqlx.DeleteStatement` or
                              :class:`mysqlx.RemoveStatement` object.
        """
        data_model = mysqlxpb_enum("Mysqlx.Crud.DataModel.DOCUMENT"
                                   if stmt.is_doc_based() else
                                   "Mysqlx.Crud.DataModel.TABLE")
        collection = Message("Mysqlx.Crud.Collection", name=stmt.target.name,
                             schema=stmt.schema.name)
        msg = Message("Mysqlx.Crud.Delete", data_model=data_model,
                      collection=collection)
        self._apply_filter(msg, stmt)
        self._writer.write_message(
            mysqlxpb_enum("Mysqlx.ClientMessages.Type.CRUD_DELETE"), msg)

    def send_execute_statement(self, namespace, stmt, args):
        """Send execute statement.

        Args:
            namespace (str): The namespace.
            stmt (Statement): A `Statement` based type object.
            args (iterable): An iterable object.
        """
        msg = Message("Mysqlx.Sql.StmtExecute", namespace=namespace, stmt=stmt,
                      compact_metadata=False)
        for arg in args:
            value = self._create_any(arg)
            msg["args"].extend([value.get_message()])
        self._writer.write_message(
            mysqlxpb_enum("Mysqlx.ClientMessages.Type.SQL_STMT_EXECUTE"), msg)

    def send_insert(self, stmt):
        """Send insert.

        Args:
            stmt (Statement): A :class:`mysqlx.AddStatement` or
                              :class:`mysqlx.InsertStatement` object.
        """
        data_model = mysqlxpb_enum("Mysqlx.Crud.DataModel.DOCUMENT"
                                   if stmt.is_doc_based() else
                                   "Mysqlx.Crud.DataModel.TABLE")
        collection = Message("Mysqlx.Crud.Collection",
                             name=stmt.target.name,
                             schema=stmt.schema.name)
        msg = Message("Mysqlx.Crud.Insert", data_model=data_model,
                      collection=collection)

        if hasattr(stmt, "_fields"):
            for field in stmt._fields:
                expr = ExprParser(field, not stmt.is_doc_based()) \
                    .parse_table_insert_field()
                msg["projection"].extend([expr.get_message()])

        for value in stmt.get_values():
            row = Message("Mysqlx.Crud.Insert.TypedRow")
            if isinstance(value, list):
                for val in value:
                    row["field"].extend([build_expr(val).get_message()])
            else:
                row["field"].extend([build_expr(value).get_message()])
            msg["row"].extend([row.get_message()])

        msg["upsert"] = stmt.is_upsert()
        self._writer.write_message(
            mysqlxpb_enum("Mysqlx.ClientMessages.Type.CRUD_INSERT"), msg)

    def close_result(self, result):
        """Close the result.

        Args:
            result (Result): A `Result` based type object.

        Raises:
            :class:`mysqlx.OperationalError`: If message read is None.
        """
        msg = self._read_message(result)
        if msg is not None:
            raise OperationalError("Expected to close the result")

    def read_row(self, result):
        """Read row.

        Args:
            result (Result): A `Result` based type object.
        """
        msg = self._read_message(result)
        if msg is None:
            return None
        if msg.type == "Mysqlx.Resultset.Row":
            return msg
        self._reader.push_message(msg)
        return None

    def get_column_metadata(self, result):
        """Returns column metadata.

        Args:
            result (Result): A `Result` based type object.

        Raises:
            :class:`mysqlx.InterfaceError`: If unexpected message.
        """
        columns = []
        while True:
            msg = self._read_message(result)
            if msg is None:
                break
            if msg.type == "Mysqlx.Resultset.Row":
                self._reader.push_message(msg)
                break
            if msg.type != "Mysqlx.Resultset.ColumnMetaData":
                raise InterfaceError("Unexpected msg type")
            col = ColumnMetaData(msg["type"], msg["catalog"], msg["schema"],
                                 msg["table"], msg["original_table"],
                                 msg["name"], msg["original_name"],
                                 msg["length"], msg["collation"],
                                 msg["fractional_digits"], msg["flags"],
                                 msg.get("content_type"))
            columns.append(col)
        return columns

    def read_ok(self):
        """Read OK.

        Raises:
            :class:`mysqlx.InterfaceError`: If unexpected message.
        """
        msg = self._reader.read_message()
        if msg.type == "Mysqlx.Error":
            raise InterfaceError(msg["msg"])
        if msg.type != "Mysqlx.Ok":
            raise InterfaceError("Unexpected message encountered")

    def send_connection_close(self):
        """Send connection close."""
        msg = Message("Mysqlx.Connection.Close")
        self._writer.write_message(mysqlxpb_enum(
            "Mysqlx.ClientMessages.Type.CON_CLOSE"), msg)

    def send_close(self):
        """Send close."""
        msg = Message("Mysqlx.Session.Close")
        self._writer.write_message(mysqlxpb_enum(
            "Mysqlx.ClientMessages.Type.SESS_CLOSE"), msg)
