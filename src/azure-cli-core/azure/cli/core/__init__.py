# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------
from __future__ import print_function

__version__ = "2.0.21"

import os
import sys

from knack.arguments import ArgumentsContext
from knack.cli import CLI
from knack.commands import CLICommandsLoader
from knack.completion import ARGCOMPLETE_ENV_NAME
from knack.introspection import extract_args_from_signature, extract_full_summary_from_signature
from knack.log import get_logger
from knack.util import CLIError

import six

logger = get_logger(__name__)


class AzCli(CLI):

    def __init__(self, **kwargs):
        super(AzCli, self).__init__(**kwargs)

        from azure.cli.core.commands.arm import add_id_parameters
        from azure.cli.core.cloud import get_active_cloud
        from azure.cli.core.extensions import register_extensions
        from azure.cli.core._session import ACCOUNT, CONFIG, SESSION

        import knack.events as events

        self.data['headers'] = {}
        self.data['command'] = 'unknown'
        self.data['command_extension_name'] = None
        self.data['completer_active'] = ARGCOMPLETE_ENV_NAME in os.environ
        self.data['query_active'] = False

        azure_folder = self.config.config_dir
        ACCOUNT.load(os.path.join(azure_folder, 'azureProfile.json'))
        CONFIG.load(os.path.join(azure_folder, 'az.json'))
        SESSION.load(os.path.join(azure_folder, 'az.sess'), max_age=3600)
        self.cloud = get_active_cloud(self)
        logger.debug('Current cloud config:\n%s', str(self.cloud.name))

        register_extensions(self)
        self.register_event(events.EVENT_INVOKER_POST_CMD_TBL_CREATE, add_id_parameters)
        # TODO: Doesn't work because args get copied
        # self.register_event(events.EVENT_INVOKER_PRE_CMD_TBL_CREATE, _pre_command_table_create)

        self.progress_controller = None

    def refresh_request_id(self):
        """Assign a new random GUID as x-ms-client-request-id

        The method must be invoked before each command execution in order to ensure
        unique client-side request ID is generated.
        """
        import uuid
        self.data['headers']['x-ms-client-request-id'] = str(uuid.uuid1())

    def get_progress_controller(self, det=False):
        import azure.cli.core.commands.progress as progress
        if not self.progress_controller:
            self.progress_controller = progress.ProgressHook()

        self.progress_controller.init_progress(progress.get_progress_view(det))
        return self.progress_controller

    def show_version(self):
        from azure.cli.core.util import get_az_version_string
        print(get_az_version_string())


class MainCommandsLoader(CLICommandsLoader):
    def __init__(self, cli_ctx=None):
        super(MainCommandsLoader, self).__init__(cli_ctx)
        self.cmd_to_loader_map = {}
        self.cmd_to_mod_map = {}
        self.loaders = []
        self.accumulate_time = 0

    def _update_command_definitions(self):
        pass

    def _load_commands_from_command_module(self, module_args, module_name):
        from azure.cli.core.util import PerformanceMonitor
        from azure.cli.core.commands import load_command_loader

        with PerformanceMonitor(self, "Loading module {}".format(module_name)):
            try:
                result = load_command_loader(self, module_args, module_name, 'azure.cli.command_modules.')
                self.command_table.update(result)
                self.cmd_to_mod_map.update({cmd: module_name for cmd in result})
                return result
            except ModuleNotFoundError:
                logger.debug('Failed to load command module %s.', module_name)

    def _load_commands_from_extension(self, module_args, extension_name):
        from azure.cli.core.util import PerformanceMonitor
        from azure.cli.core.commands import load_command_loader, ExtensionCommandSource

        with PerformanceMonitor(self, "Loading extension {}".format(extension_name)):
            try:
                result = load_command_loader(self, module_args, extension_name, '')

                for cmd_name, cmd in result.items():
                    cmd.command_source = ExtensionCommandSource(
                        extension_name=extension_name,
                        overrides_command=cmd_name in self.cmd_to_mod_map)
                self.command_table.update(result)

                return result
            except ModuleNotFoundError:
                logger.debug('Failed to load extension %s.', extension_name)

    def _update_command_table_from_modules(self, args):
        """
        Loads command definitions from the command modules (aside from extensions). The command definitions loaded
        in this function will not necessarily all be added to the argparse as subparsers.

        The module_args is expected to be a list of string presents the sub commands in the order as user input. The
        first string in the module_args is called root command here. The root command must be same as the name of
        the command module which carries this command.

        The the module "azure.cli.command_modules.{root_command} is failed to be located, or there isn't any command
        found in this module this method will fallback to load all modules under the "azure.cli.command_modules"
        namespace, thus hurt the performance.

        If the module_args is empty, meaning the user simply type "az", all the modules will be loaded as well.
        """
        import traceback
        from pkgutil import iter_modules
        from importlib import import_module
        from azure.cli.core.commands import BLACKLISTED_MODS

        root_command = args[0] if args else None
        if root_command and root_command not in BLACKLISTED_MODS:
            logger.debug('Try loading command modules %s', root_command)
            self._load_commands_from_command_module(args, root_command)
            if self.command_table:
                # some command definitions have been load
                return

        # fallback when args is empty or the root command is not mapped to any modules
        # Note: this part can be further optimized by delaying after the extension modules are lod.
        try:
            mods_ns_pkg = import_module('azure.cli.command_modules')
            installed_command_modules = [modname for _, modname, _ in iter_modules(mods_ns_pkg.__path__)
                                         if modname not in BLACKLISTED_MODS]
        except ImportError:
            installed_command_modules = []

        logger.debug('Find command modules %s', installed_command_modules)
        for mod in installed_command_modules:
            try:
                self._load_commands_from_command_module(args, mod)
            except Exception as ex:  # pylint: disable=broad-except
                # Changing this error message requires updating CI script that checks for failed
                # module loading.
                import azure.cli.core.telemetry as telemetry
                logger.error("Error loading command module '%s'", mod)
                telemetry.set_exception(exception=ex, fault_type='module-load-error-' + mod,
                                        summary='Error loading module: {}'.format(mod))
                logger.debug(traceback.format_exc())
        logger.debug('Found and load %d command definitions', len(self.command_table))

    def _update_command_table_from_extensions(self, args):
        """
        We always load extensions even if the appropriate module has been loaded as an extension could override the
        commands already loaded.
        """
        import traceback
        from azure.cli.core.extension import get_extension_names, get_extension_path, get_extension_modname
        from azure.cli.core.commands import ExtensionCommandSource

        try:
            extensions = get_extension_names()
            if extensions:
                logger.debug("Found %s extensions: %s", len(extensions), extensions)
                for ext_name in extensions:
                    ext_dir = get_extension_path(ext_name)
                    sys.path.append(ext_dir)
                    try:
                        ext_mod = get_extension_modname(ext_name, ext_dir=ext_dir)
                        # Add to the map. This needs to happen before we load commands as registering a command
                        # from an extension requires this map to be up-to-date.
                        # self._mod_to_ext_map[ext_mod] = ext_name
                        self._load_commands_from_extension(args, ext_mod)
                    except Exception:  # pylint: disable=broad-except
                        logger.warning("Unable to load extension '%s'. Use --debug for more information.", ext_name)
                        logger.debug(traceback.format_exc())
        except Exception:  # pylint: disable=broad-except
            logger.warning("Unable to load extensions. Use --debug for more information.")
            logger.debug(traceback.format_exc())

    def load_command_table(self, args):
        self._update_command_table_from_modules(args)
        self._update_command_table_from_extensions(args)

        get_logger('PERF').debug("Loaded all modules and extensions in %.3f seconds. (note: there's always an overhead "
                                 "with the first module loaded)", self.accumulate_time)

        return self.command_table

    def load_arguments(self, command):
        from azure.cli.core.commands.parameters import resource_group_name_type, get_location_type, deployment_name_type
        from knack.arguments import ignore_type

        command_loaders = self.cmd_to_loader_map.get(command, None)

        if command_loaders:
            for loader in command_loaders:
                loader.load_arguments(command)
                self.argument_registry.arguments.update(loader.argument_registry.arguments)
                self.extra_argument_registry.update(loader.extra_argument_registry)

            with ArgumentsContext(self, '') as c:
                c.argument('resource_group_name', resource_group_name_type)
                c.argument('location', get_location_type(self.cli_ctx))
                c.argument('deployment_name', deployment_name_type)
                c.argument('cmd', ignore_type)

            super(MainCommandsLoader, self).load_arguments(command)


class AzCommandsLoader(CLICommandsLoader):

    def __init__(self, cli_ctx=None, min_profile=None, max_profile='latest', **kwargs):
        from azure.cli.core.commands import AzCliCommand
        super(AzCommandsLoader, self).__init__(cli_ctx=cli_ctx, command_cls=AzCliCommand)
        self.module_name = __name__
        self.min_profile = min_profile
        self.max_profile = max_profile
        self.module_kwargs = kwargs

    def _update_command_definitions(self):
        for command_name, command in self.command_table.items():
            for argument_name in command.arguments:
                overrides = self.argument_registry.get_cli_argument(command_name, argument_name)
                command.update_argument(argument_name, overrides)

            # Add any arguments explicitly registered for this command
            for argument_name, argument_definition in self.extra_argument_registry[command_name].items():
                command.arguments[argument_name] = argument_definition
                command.update_argument(argument_name,
                                        self.argument_registry.get_cli_argument(command_name, argument_name))

    def _apply_doc_string(self, dest, command_kwargs):
        doc_string_source = command_kwargs.get('doc_string_source', None)
        if not doc_string_source:
            return
        elif not isinstance(doc_string_source, str):
            raise CLIError("command authoring error: applying doc_string_source '{}' directly will cause slowdown. "
                           'Import by string name instead.'.format(doc_string_source.__name__))

        model = doc_string_source
        try:
            model = self.get_models(doc_string_source)
        except AttributeError:
            model = None
        if not model:
            from importlib import import_module
            (path, model_name) = doc_string_source.split('#', 1)
            method_name = None
            if '.' in model_name:
                (model_name, method_name) = model_name.split('.', 1)
            module = import_module(path)
            model = getattr(module, model_name)
            if method_name:
                model = getattr(model, method_name, None)
        if not model:
            raise CLIError("command authoring error: source '{}' not found.".format(doc_string_source))
        dest.__doc__ = model.__doc__


    def get_api_version(self, resource_type=None):
        from azure.cli.core.profiles import get_api_version
        resource_type = resource_type or self.module_kwargs.get('resource_type', None)
        return get_api_version(self.cli_ctx, resource_type)

    def supported_api_version(self, resource_type=None, min_api=None, max_api=None):
        from azure.cli.core.profiles import supported_api_version, PROFILE_TYPE
        return supported_api_version(
            cli_ctx=self.cli_ctx,
            resource_type=resource_type or PROFILE_TYPE,
            min_api=min_api or self.min_profile,
            max_api=max_api or self.max_profile)

    def get_sdk(self, *attr_args, **kwargs):
        from azure.cli.core.profiles import get_sdk
        return get_sdk(self.cli_ctx, kwargs.pop('resource_type', self.module_kwargs['resource_type']),
                       *attr_args, **kwargs)

    def get_models(self, *attr_args, **kwargs):
        resource_type = kwargs.get('resource_type', self.module_kwargs.get('resource_type', None))
        from azure.cli.core.profiles import get_sdk
        return get_sdk(self.cli_ctx, resource_type, *attr_args, mod='models')

    def command_group(self, group_name, command_type=None, **kwargs):
        from azure.cli.core.sdk.util import _CommandGroup
        merged_kwargs = self.module_kwargs.copy()
        if command_type:
            merged_kwargs['command_type'] = command_type
        merged_kwargs.update(kwargs)
        return _CommandGroup(self.module_name, self, group_name, **merged_kwargs)

    def argument_context(self, scope, **kwargs):
        from azure.cli.core.sdk.util import _ParametersContext
        merged_kwargs = self.module_kwargs.copy()
        merged_kwargs.update(kwargs)
        return _ParametersContext(self, scope, **merged_kwargs)

    def _cli_command(self, name, operation=None, handler=None, argument_loader=None, description_loader=None, **kwargs):

        if operation and not isinstance(operation, six.string_types):
            raise TypeError("Operation must be a string. Got '{}'".format(operation))
        if handler and not callable(handler):
            raise TypeError("Handler must be a callable. Got '{}'".format(operation))
        if bool(operation) == bool(handler):
            raise TypeError("Must specify exactly one of either 'operation' or 'handler'")

        name = ' '.join(name.split())

        client_factory = kwargs.get('client_factory', None)

        def default_command_handler(command_args):
            op = handler or self.get_op_handler(operation)

            client = client_factory(self.cli_ctx, command_args) if client_factory else None
            if client:
                client_arg_name = 'client' if operation.startswith('azure.cli') else 'self'
                command_args[client_arg_name] = client
            result = op(**command_args)
            return result

        def default_arguments_loader():
            op = handler or self.get_op_handler(operation)
            self._apply_doc_string(op, kwargs)
            cmd_args = list(extract_args_from_signature(op))
            return cmd_args

        def default_description_loader():
            op = handler or self.get_op_handler(operation)
            self._apply_doc_string(op, kwargs)
            return extract_full_summary_from_signature(op)

        kwargs['arguments_loader'] = argument_loader or default_arguments_loader
        kwargs['description_loader'] = description_loader or default_description_loader

        if self.supported_api_version(resource_type=kwargs.get('resource_type'),
                                      min_api=kwargs.get('min_api'),
                                      max_api=kwargs.get('max_api')):
            self.command_table[name] = self.command_cls(self, name,
                                                        handler or default_command_handler,
                                                        **kwargs)

    def get_op_handler(self, operation):
        """ Import and load the operation handler """
        # Patch the unversioned sdk path to include the appropriate API version for the
        # resource type in question.
        from importlib import import_module
        import types

        from azure.cli.core.profiles import ResourceType
        from azure.cli.core.profiles._shared import get_versioned_sdk_path

        for rt in ResourceType:
            if operation.startswith(rt.import_prefix):
                operation = operation.replace(rt.import_prefix,
                                              get_versioned_sdk_path(self.cli_ctx.cloud.profile, rt))

        try:
            mod_to_import, attr_path = operation.split('#')
            op = import_module(mod_to_import)
            for part in attr_path.split('.'):
                op = getattr(op, part)
            if isinstance(op, types.FunctionType):
                return op
            return six.get_method_function(op)
        except (ValueError, AttributeError):
            raise ValueError("The operation '{}' is invalid.".format(operation))
