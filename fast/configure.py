#!/usr/bin/env python3

# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import re
import sys
import time
import shutil
import subprocess
import tempfile
import asyncio
import glob
import fnmatch
from pathlib import Path

try:
    import json
    from prompt_toolkit import PromptSession, shortcuts
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.application import Application
    from prompt_toolkit.application.current import get_app
    from prompt_toolkit.key_binding.defaults import load_key_bindings
    from prompt_toolkit.key_binding.key_bindings import KeyBindings, merge_key_bindings
    from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
    from prompt_toolkit.layout.containers import HSplit, VSplit
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.validation import Validator
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.widgets import (
        Button,
        Dialog,
        Label,
        TextArea,
        ValidationToolbar,
        RadioList,
    )
    from prompt_toolkit.cursor_shapes import CursorShape
    from googleapiclient import discovery, http
    import google_auth_httplib2
    import google.auth
    import gitlab
    from github import Github

except ImportError:
    print(
        "Looks like don't have some of the Python dependencies installed,\nplease install the requirements: pip install -r requirements.txt"
    )
    sys.exit(1)

VERSION = "0.1.0"

GIT = shutil.which("git")
if not GIT:
    print(
        "You don't have Git installed (how did you get here?), please install it."
    )
    sys.exit(1)

TERRAFORM = shutil.which("terraform")
if not TERRAFORM:
    print("You don't have Terraform installed, please install it.")
    sys.exit(1)

FABRIC_REPOSITORY = "GoogleCloudPlatform/cloud-foundation-fabric"


class FastDialogs:
    session = None
    application = None
    outputs_path = None
    indent_spaces = 2
    fabric_repository = "https://github.com/GoogleCloudPlatform/cloud-foundation-fabric.git"
    config = {
        "use_upstream": True,
        "upstream_tag": None,
        "bootstrap_output_path": None,
        "cicd_output_path": None,
    }
    repositories = {
        "modules": "Fabric modules",
        "bootstrap": "Bootstrap stage",
        "cicd": "CI/CD setup stage",
        "resman": "Resource management stage",
        "networking": "Networking setup stage",
        "security": "Security configuration stage",
        "data-platform": "Data platform stage",
        "project-factory": "Project factory stage",
    }

    repository_stages = {
        "modules": None,
        "bootstrap": "00-bootstrap",
        "cicd": "00-cicd",
        "resman": "01-resman",
        "networking": "02-networking-",
        "security": "02-security",
        "data-platform": "03-data-platform",
        "project-factory": "03-project-factory",
    }

    repository_dependencies = {
        "modules": [],
        "bootstrap": [],
        "cicd": ["00-bootstrap"],
        "resman": ["00-bootstrap"],
        "networking": ["00-bootstrap", "01-resman"],
        "security": ["00-bootstrap", "01-resman"],
        "data-platform": [],
        "project-factory": [],
    }

    modules_include = [
        "LICENSE",
        "*.md",
        "*.tf",
        "*.png",
        "assets",
        "examples",
        "modules",
        "tests",
        "tools",
    ]

    def __init__(self, session):
        self.session = session
        if self.outputs_path is None:
            self.outputs_path = os.getcwd() + "/outputs"
            print(f"Using path for outputs: {self.outputs_path}")
            if not os.path.isdir(self.outputs_path):
                os.mkdir(self.outputs_path)

    def maybe_quit(self):
        result = shortcuts.yes_no_dialog(
            title='Quit configuration process',
            text='Do you want to quit the configuration?').run()
        if result:
            quit()

    def get_current_user_from_gcloud(self):
        result = subprocess.run(
            ["gcloud", "config", "list", "--format", "value(core.account)"],
            capture_output=True)
        return result.stdout.decode("utf-8").strip() + result.stderr.decode(
            "utf-8").strip()

    def get_branded_http(self):
        credentials, project_id = google.auth.default(
            ['https://www.googleapis.com/auth/cloud-platform'])
        branded_http = google_auth_httplib2.AuthorizedHttp(credentials)
        branded_http = http.set_user_agent(
            branded_http,
            f"google-pso-tool/cloud-foundation-fabric/configure/v1.0.0")
        return branded_http

    def terraform_format(self, filename):
        result = subprocess.run(["terraform", "fmt", filename])
        if result.returncode != 0:
            raise Exception(f"Unable to terraform fmt {filename}!")

    def change_module_source(self, filename, target_repository, tag=None):

        def change_module_source_sub(matchobj):
            module_source = matchobj.group(2)
            if "../modules" in module_source:
                module_source = module_source.replace("../", "")
                module_source = f"git::{target_repository}//" + module_source
                if tag:
                    module_source += f"?ref={tag}"
            return matchobj.group(1) + module_source + matchobj.group(3)

        temp_f = tempfile.NamedTemporaryFile(delete=False)
        source_re = re.compile(r"(source[\s]*=[\s]*\")(.+?)(\")")
        with open(filename, "r") as f:
            while True:
                line = f.readline()
                if not line:
                    break
                line = re.sub(source_re, change_module_source_sub, line)
                temp_f.write(line.encode("utf-8"))
            temp_f.close()
        shutil.move(temp_f.name, filename)

    async def read_subprocess(self, stream, cb):
        while True:
            line = await stream.readline()
            if line:
                cb(line)
            else:
                break

    def read_subprocess_stdout(self, line):
        if line:
            line_decoded = line.decode("utf-8")
            self.tf_stdout += line_decoded
            self.output_label.text += line_decoded
            self.output_label.document._cursor_position = len(
                self.output_label.text)
            self.tf_app.invalidate()

    def read_subprocess_stderr(self, line):
        if line:
            line_decoded = line.decode("utf-8")
            self.tf_stderr += line_decoded
            self.output_label.text += line_decoded
            self.output_label.document._cursor_position = len(
                self.output_label.text)
            self.tf_app.invalidate()

    async def async_run_terraform(self, cmd, dir, stdout_cb, stderr_cb):
        os.environ["TF_IN_AUTOMATION"] = "1"
        os.environ["TF_INPUT"] = "0"
        os.environ["TF_CLI_ARGS"] = "-no-color"
        p = await asyncio.subprocess.create_subprocess_shell(
            " ".join(cmd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=dir)
        await asyncio.wait([
            self.read_subprocess(p.stdout, stdout_cb),
            self.read_subprocess(p.stderr, stderr_cb)
        ])
        self.return_code = await p.wait()
        if self.return_code != 0:
            self.ok_button.text = "Retry"
        else:
            self.ok_button.text = "Ok"
        self.tf_app.invalidate()
        return self.return_code

    async def run_tasks(self, cmd, dir, app):
        tasks = []
        tasks.append(
            asyncio.ensure_future(
                self.async_run_terraform(cmd, dir, self.read_subprocess_stdout,
                                         self.read_subprocess_stderr)))
        tasks.append(app.run_async())
        r = await asyncio.gather(*tasks)
        return r

    def run_terraform_until_ok(self, cmd, dir):

        def ok_handler():
            if self.ok_button.text != "Wait...":
                get_app().exit(result=True)

        def previous_handler():
            get_app().exit(result=False)

        def quit_handler():
            get_app().exit(result=None)

        cmd_joined = " ".join(cmd)
        self.ok_button = Button(text="Wait...", handler=ok_handler)
        self.output_label = TextArea(text="", read_only=True, line_numbers=True)
        dialog = Dialog(
            title="Running: Terraform",
            body=HSplit(
                [
                    Label(text=f"Running {cmd_joined}:",
                          dont_extend_height=True), self.output_label
                ],
                padding=1,
            ),
            buttons=[
                self.ok_button,
                Button(text="Previous", handler=previous_handler),
                Button(text="Quit", handler=quit_handler),
            ],
            with_background=True,
        )

        while True:
            print(f"Running Terraform in {dir}: {cmd_joined}")
            self.ok_button.text = "Wait..."
            self.output_label.text = ""

            self.tf_stdout = self.tf_stderr = ""

            self.tf_app = self._create_app(dialog, style=None)

            loop = asyncio.get_event_loop()
            return_code, result = loop.run_until_complete(
                self.run_tasks(cmd, dir, self.tf_app))
            if result is None:
                self.maybe_quit()
            if return_code == 0 and result:
                return True
            if not result:
                return False

    def run_terraform_with_output(self, cmd, dir):
        result = subprocess.run(cmd,
                                cwd=dir,
                                capture_output=True,
                                text=True,
                                check=True)
        return result.stdout, result.stderr

    def terraform_init(self, dir):
        cmd = ["terraform", "init", "-migrate-state", "-force-copy"]
        return self.run_terraform_until_ok(cmd, dir)

    def terraform_plan(self, dir, flags=None):
        cmd = ["terraform", "plan"]
        if flags:
            cmd = cmd + flags
        return self.run_terraform_until_ok(cmd, dir)

    def terraform_apply(self, dir, flags=None):
        cmd = ["terraform", "apply", "-auto-approve"]
        if flags:
            cmd = cmd + flags
        return self.run_terraform_until_ok(cmd, dir)

    def terraform_output(self, dir):
        cmd = ["terraform", "output", "-json"]
        output_stdout, output_stderr = self.run_terraform_with_output(cmd, dir)
        return output_stdout

    def run_git(self, cmd, dir, allow_fail=False):
        try:
            result = subprocess.run(["git"] + cmd,
                                    cwd=dir,
                                    capture_output=True,
                                    text=True,
                                    check=True)
        except subprocess.CalledProcessError as e:
            if allow_fail:
                return None, None
            print(f"Command {e.cmd} failed: {e.stdout} {e.stderr}")
            raise e
        return result.stdout, result.stderr

    def select_upstream_modules(self):
        g = Github()
        repo = g.get_repo(FABRIC_REPOSITORY)
        tags = repo.get_tags()
        latest_tag = tags[0].name

        (got_choice, choice) = self.yesno_dialog(
            "modules", f"Use upstream modules?",
            f"Would you like to use the upstream Fabric modules at version {latest_tag}?\nOtherwise we will fork the modules into your own repository."
        )
        if got_choice:
            self.config["use_upstream"] = choice
            self.config["upstream_tag"] = latest_tag
        return got_choice

    def select_dialog(self, id, title, text, values, value):

        def ok_handler():
            get_app().exit(result=radio_list.current_value)

        def previous_handler():
            get_app().exit(result=False)

        def quit_handler():
            get_app().exit()

        tuple_values = []
        for k, v in values.items():
            tuple_values.append((k, v))
        radio_list = RadioList(values=tuple_values, default=value)
        dialog = Dialog(
            title=title,
            body=HSplit(
                [Label(text=text, dont_extend_height=True), radio_list],
                padding=1,
            ),
            buttons=[
                Button(text="Ok", handler=ok_handler),
                Button(text="Previous", handler=previous_handler),
                Button(text="Quit", handler=quit_handler),
            ],
            with_background=True,
        )
        radio_list_app = self._create_app(dialog, style=None)
        while True:
            choice = radio_list_app.run()
            if choice is None:
                self.maybe_quit()
            else:
                if choice is False:
                    return (False, None)
                return (True, choice)

    def select_with_input_dialog(self, id, title, text, new_text, values,
                                 value):

        def ok_handler():
            if radio_list.current_value == "_":
                get_app().exit(result=textfield.text)
            else:
                get_app().exit(result=radio_list.current_value)

        def previous_handler():
            get_app().exit(result=False)

        def accept_handler(buf):
            get_app().layout.focus(ok_button)
            return True

        def quit_handler():
            get_app().exit()

        tuple_values = []
        in_radiolist = False
        for k, v in values.items():
            if k == value:
                in_radiolist = True
            tuple_values.append((k, v))
        radio_list = RadioList(values=tuple_values, default=value)
        textfield = TextArea(
            text="",
            multiline=False,
            accept_handler=accept_handler,
        )

        ok_button = Button(text="Ok", handler=ok_handler)
        dialog = Dialog(
            title=title,
            body=HSplit(
                [
                    Label(text=text, dont_extend_height=True), radio_list,
                    Label(text=new_text, dont_extend_height=True), textfield
                ],
                padding=1,
            ),
            buttons=[
                ok_button,
                Button(text="Previous", handler=previous_handler),
                Button(text="Quit", handler=quit_handler),
            ],
            with_background=True,
        )
        radio_list_app = self._create_app(dialog, style=None)
        while True:
            choice = radio_list_app.run()
            if choice is None:
                self.maybe_quit()
            else:
                if choice is False:
                    return (False, None)
                return (True, choice)

    def multi_input_dialog(self, id, title, text, values, value):

        def ok_handler():
            ret = {}
            for k, v in textfields.items():
                ret[k] = v.text
            get_app().exit(result=ret)

        def previous_handler():
            get_app().exit(result=False)

        def accept_handler(buf):
            get_app().layout.focus_next()
            return True

        def quit_handler():
            get_app().exit()

        textfields = {}
        controls = []
        for k, v in values.items():
            textfields[k] = TextArea(
                text=value[k],
                multiline=False,
                accept_handler=accept_handler,
            )
            controls.append(Label(text=v, dont_extend_height=True))
            controls.append(textfields[k])

        dialog = Dialog(
            title=title,
            body=HSplit(
                [Label(text=text, dont_extend_height=True)] + controls,
                padding=1,
            ),
            buttons=[
                Button(text="Ok", handler=ok_handler),
                Button(text="Previous", handler=previous_handler),
                Button(text="Quit", handler=quit_handler),
            ],
            with_background=True,
        )
        multi_input_app = self._create_app(dialog, style=None)
        while True:
            choice = multi_input_app.run()
            if choice is None:
                self.maybe_quit()
            else:
                if choice is False:
                    return (False, None)
                return (True, choice)

    def confirm_dialog(self, id, title, text):

        def ok_handler():
            get_app().exit(result=True)

        def previous_handler():
            get_app().exit(result=False)

        def quit_handler():
            get_app().exit()

        dialog = Dialog(
            title=title,
            body=Label(text=text, dont_extend_height=True),
            buttons=[
                Button(text="Ok", handler=ok_handler),
                Button(text="Previous", handler=previous_handler),
                Button(text="Quit", handler=quit_handler),
            ],
            with_background=True,
        )
        confirm_app = self._create_app(dialog, style=None)
        while True:
            choice = confirm_app.run()
            if choice is None:
                self.maybe_quit()
            else:
                return choice

    def input_dialog(self,
                     id,
                     title,
                     text,
                     value="",
                     validator=None,
                     password=False):

        def accept_handler(buf):
            get_app().layout.focus(ok_button)
            return True

        def ok_handler():
            get_app().exit(result=textfield.text)

        def previous_handler():
            get_app().exit(result=False)

        def quit_handler():
            get_app().exit()

        ok_button = Button(text="Ok", handler=ok_handler)
        textfield = TextArea(
            text=value,
            multiline=False,
            password=password,
            validator=validator,
            accept_handler=accept_handler,
        )
        dialog = Dialog(
            title=title,
            body=HSplit([
                Label(text=text, dont_extend_height=True),
                textfield,
                ValidationToolbar(),
            ]),
            buttons=[
                ok_button,
                Button(text="Previous", handler=previous_handler),
                Button(text="Quit", handler=quit_handler),
            ],
            with_background=True,
        )
        input_app = self._create_app(dialog, style=None)
        while True:
            choice = input_app.run()
            if choice is None:
                self.maybe_quit()
            else:
                return choice

    def yesno_dialog(self, id, title, text):

        def yes_handler():
            get_app().exit(result=(True, True))

        def no_handler():
            get_app().exit(result=(True, False))

        def previous_handler():
            get_app().exit(result=(False, None))

        def quit_handler():
            get_app().exit(result=(None, None))

        dialog = Dialog(
            title=title,
            body=Label(text=text, dont_extend_height=True),
            buttons=[
                Button(text="Yes", handler=yes_handler),
                Button(text="No", handler=no_handler),
                Button(text="Previous", handler=previous_handler),
                Button(text="Quit", handler=quit_handler),
            ],
            with_background=True,
        )
        yesno_app = self._create_app(dialog, style=None)
        while True:
            (got_choice, choice) = yesno_app.run()
            if got_choice is None:
                self.maybe_quit()
            else:
                return (got_choice, choice)

    def _create_app(self, dialog, style):
        bindings = KeyBindings()
        bindings.add("tab")(focus_next)
        bindings.add("s-tab")(focus_previous)

        return Application(
            layout=Layout(dialog),
            key_bindings=merge_key_bindings([load_key_bindings(), bindings]),
            mouse_support=False,
            full_screen=True,
            cursor=CursorShape.BLINKING_BLOCK,
        )

    def print_hcl_scalar(self, f, scalar):
        if scalar is None:
            f.write("null")
        if isinstance(scalar, str):
            f.write("\"" + scalar.replace("\"", "\\\"") + "\"")
        elif isinstance(scalar, bool):
            f.write("true" if scalar is True else "false")
        elif isinstance(scalar, int) or isinstance(scalar, float):
            f.write(str(scalar))
        elif isinstance(scalar, list):
            first = True
            f.write("[")
            for v in scalar:
                if not first:
                    f.write(", ")
                self.print_hcl_scalar(f, v)
                first = False
            f.write("]")

    def print_hcl(self, content, f, indent=0):
        if isinstance(content, dict):
            f.write((indent * self.indent_spaces) * " ")
            if indent != 0:
                f.write("{\n")
            for k, v in content.items():
                f.write((indent * self.indent_spaces) * " ")
                f.write(f"{k} = ")
                self.print_hcl(v, f, indent + 1)
            f.write((indent * self.indent_spaces) * " ")
            if indent != 0:
                f.write("}\n")
        else:
            self.print_hcl_scalar(f, content)
        f.write("\n")

    def write_cicd_config(self, filename):
        with open(filename, "w") as f:
            cicd_config = {
                "cicd_repositories": {},
                "outputs_location": self.outputs_path,
            }
            if "gitlab" in self.config["cicd"]:

                self.config["bootstrap_identity_providers"] = {
                    "gitlab": {
                        "attribute_condition":
                            f"attribute.namespace_path==\"{self.config['gitlab_group']}\"",
                        "issuer":
                            "gitlab",
                        "custom_settings":
                            None,
                    }
                }

                if self.config["cicd"] == "gitlab-ce":
                    self.config["bootstrap_identity_providers"]["gitlab"][
                        "custom_settings"] = {
                            "issuer_uri": self.config["gitlab_url"],
                            "allowed_audiences": [self.config["gitlab_url"]]
                        }

                self.config["bootstrap_repositories"] = {
                    "bootstrap": {
                        "branch": "main",
                        "identity_provider": "gitlab",
                        "name": "bootstrap",
                        "type": "gitlab"
                    },
                    "cicd": {
                        "branch": "main",
                        "identity_provider": "gitlab",
                        "name": "cicd",
                        "type": "gitlab"
                    },
                    "resman": {
                        "branch": "main",
                        "identity_provider": "gitlab",
                        "name": "resman",
                        "type": "gitlab"
                    },
                }

                cicd_config["gitlab"] = {
                    "url":
                        "https://gitlab.com" if self.config["cicd"]
                        == "gitlab-com" else self.config["gitlab_url"],
                    "project_visibility":
                        "private",
                    "shared_runners_enabled":
                        True,
                }
                if "modules" not in self.config["gitlab_repositories"]:
                    cicd_config["cicd_repositories"]["modules"] = None

                for k, v in self.config["gitlab_repositories"].items():
                    cicd_config["cicd_repositories"][k] = {
                        "branch": "main",
                        "identity_provider": "gitlab",
                        "name": f"{self.config['gitlab_group']}/{v}",
                        "description": self.repositories[v],
                        "type": "gitlab",
                        "create": True,
                        "create_group": self.config["gitlab_group_create"],
                    }

            if self.config["cicd"] == "github":
                self.config["bootstrap_identity_providers"] = {
                    "github": {
                        "attribute_condition":
                            f"attribute.namespace_path==\"{self.config['github_organization']}\"",
                        "issuer":
                            "github",
                        "custom_settings":
                            None,
                    }
                }

                self.config["bootstrap_repositories"] = {
                    "bootstrap": {
                        "branch": "main",
                        "identity_provider": "github",
                        "name": "bootstrap",
                        "type": "github"
                    },
                    "cicd": {
                        "branch": "main",
                        "identity_provider": "github",
                        "name": "cicd",
                        "type": "github"
                    },
                    "resman": {
                        "branch": "main",
                        "identity_provider": "github",
                        "name": "resman",
                        "type": "github"
                    },
                }

                cicd_config["github"] = {
                    "url": None,
                    "visibility": "private",
                }
                if "modules" not in self.config["github_repositories"]:
                    cicd_config["cicd_repositories"]["modules"] = None

                for k, v in self.config["github_repositories"].items():
                    cicd_config["cicd_repositories"][k] = {
                        "branch": "main",
                        "identity_provider": "github",
                        "name": f"{self.config['github_organization']}/{v}",
                        "description": self.repositories[v],
                        "type": "github",
                        "create": True,
                        "create_group": False,
                    }

            f.write("# Written by configure.py\n\n")
            self.print_hcl(cicd_config, f)
        self.terraform_format(filename)

    def write_bootstrap_config(self, filename):
        with open(filename, "w") as f:
            ba_id = self.config["billing_account"].split("/")[-1]
            ba_org = self.config["billing_account_org"].split("/")[-1]
            org = self.config["organization"].split("/")[-1]
            domain = self.config["domain"]
            directory_customer_id = self.config["directory_customer_id"]
            prefix = self.config["prefix"]

            bootstrap_config = {
                "billing_account": {
                    "id": ba_id,
                    "organization_id": ba_org,
                },
                "organization": {
                    "id": org,
                    "domain": domain,
                    "customer_id": directory_customer_id,
                },
                "prefix": prefix,
                "outputs_location": self.outputs_path,
                "bootstrap_user": self.get_current_user_from_gcloud(),
            }
            if "bootstrap_repositories" in self.config:
                bootstrap_config["cicd_repositories"] = self.config[
                    "bootstrap_repositories"]
            if "bootstrap_identity_providers" in self.config:
                bootstrap_config["federated_identity_providers"] = self.config[
                    "bootstrap_identity_providers"]

            f.write("# Written by configure.py\n\n")
            self.print_hcl(bootstrap_config, f)
        self.terraform_format(filename)

    def run_wizard(self):
        dialog_index = 0
        while True:
            dialog = self.setup_wizard[dialog_index]
            dialog_func = f"select_{dialog}"
            go_to_next = getattr(self, dialog_func)()
            if go_to_next:
                dialog_index += 1
            else:
                dialog_index -= 1
            if dialog_index == len(self.setup_wizard):
                break
        return self.config


class FastCicdConfigurator(FastDialogs):
    config = {"git": "ssh"}
    setup_wizard = ["terraform", "git"]
    to_include = [
        "*.tf",
        "*.md",
        "*.png",
        "*.svg",
        "*.yaml",
        "data",
        "dev",
    ]

    def __init__(self, session, config):
        super().__init__(session)
        self.config = {**self.config, **config}

    def select_terraform(self):
        (got_choice, choice) = self.yesno_dialog(
            "terraform", f"Run Terraform?",
            f"Now that CI/CD is configured, would you like me to run Terraform?"
        )
        if got_choice:
            if choice:
                for stage in ["00-bootstrap", "00-cicd"]:
                    stage_dir = f"stages/{stage}"
                    providers = self.outputs_path + f"/providers/{stage}-providers.tf"
                    if not os.path.exists(f"{stage_dir}/{stage}-providers.tf"
                                         ) and os.path.exists(providers):
                        print(f"Copying {providers} to {stage_dir}...")
                        shutil.copy2(providers, stage_dir)

                    if stage != "00-bootstrap":
                        bootstrap_auto_vars = self.outputs_path + "/tfvars/00-bootstrap.auto.tfvars.json"
                        if not os.path.exists(
                                f"{stage_dir}/00-bootstrap.auto.tfvars.json"
                        ) and os.path.exists(bootstrap_auto_vars):
                            print(
                                f"Copying {bootstrap_auto_vars} to {stage_dir}..."
                            )
                            shutil.copy2(bootstrap_auto_vars, stage_dir)

                    output_path = self.config[
                        "bootstrap_output_path"] if stage == "00-bootstrap" else self.config[
                            "cicd_output_path"]
                    self.terraform_init(os.path.dirname(output_path))
                    self.terraform_apply(os.path.dirname(output_path))
            return True
        return got_choice

    def copy_with_patterns(self, source_dir, target_dir, patterns):
        current_dir = os.getcwd()
        os.chdir(source_dir)
        for filename in glob.glob(f"*"):
            include_file = False
            filename_basename = filename
            for pattern in patterns:
                if fnmatch.fnmatch(filename_basename, pattern):
                    include_file = True
                    break
            if include_file:
                if os.path.isdir(filename):
                    shutil.copytree(filename, f"{target_dir}/{filename}")
                else:
                    shutil.copy2(filename, target_dir)
        os.chdir(current_dir)

    def select_git(self):
        tf_output_str = self.terraform_output(
            os.path.dirname(self.config["cicd_output_path"]))
        tf_outputs = json.loads(tf_output_str)
        if "tfvars" in tf_outputs:
            self.config["cicd_ssh_urls"] = tf_outputs["tfvars"]["value"][
                "cicd_ssh_urls"]
            self.config["cicd_https_urls"] = tf_outputs["tfvars"]["value"][
                "cicd_https_urls"]
            self.config["cicd_import_ok"] = True
        else:
            self.config["cicd_import_ok"] = False
        (got_choice, choice) = self.select_dialog(
            "cicd", "Push FAST to source control system?",
            "Would you like to initialize the CI/CD repositories with FAST stages?",
            {
                "ssh": "Yes, push via SSH",
                "https": "Yes, push via HTTPS",
                "no": "No, but thanks",
            }, self.config["git"])
        if got_choice:
            self.config["git"] = choice
            if self.config["git"] != "no":
                for repository, name in self.repositories.items():
                    stage = self.repository_stages[repository]
                    temp_dir = tempfile.mkdtemp(prefix=stage)
                    if stage:
                        if repository == "networking":
                            stage += self.config["networking_model"]

                        print(f"Setting up stage {stage} in: {temp_dir}")
                        self.copy_with_patterns(f"stages/{stage}", temp_dir,
                                                self.to_include)

                        provider_path = self.outputs_path + f"/providers/{stage}-providers.tf"
                        if os.path.exists(provider_path):
                            print(f"Copying {provider_path} to {temp_dir}")
                            shutil.copy2(
                                provider_path,
                                temp_dir + os.path.basename(provider_path))

                        globals_path = self.outputs_path + f"/tfvars/globals.auto.tfvars.json"
                        if os.path.exists(globals_path):
                            print(f"Copying {globals_path} to {temp_dir}")
                            shutil.copy2(
                                globals_path,
                                temp_dir + os.path.basename(globals_path))

                        workflow_path = self.outputs_path + f"/workflows/{repository}-workflow.yaml"
                        if os.path.exists(workflow_path):
                            print(f"Copying {workflow_path} to {temp_dir}")
                            shutil.copy2(workflow_path,
                                         temp_dir + ".gitlab-ci.yml")

                        for dependencies in self.repository_dependencies[
                                repository]:
                            for dependency in dependencies:
                                dependency_path = self.outputs_path + f"/tfvars/{dependency}.auto.tfvars.json"
                                if os.path.exists(dependency_path):
                                    print(
                                        f"Copying {dependency_path} to {temp_dir}"
                                    )
                                    shutil.copy2(
                                        dependency_path, temp_dir +
                                        os.path.basename(dependency_path))

                        print(f"Fixing up module sources in {temp_dir}")
                        module_repository = module_tag = None
                        if self.config["use_upstream"]:
                            module_repository = self.fabric_repository
                            module_tag = self.config["upstream_tag"]
                        else:
                            module_tag = None
                            if self.config["git"] == "ssh":
                                module_repository = self.config[
                                    "cicd_ssh_urls"]["modules"]
                            else:
                                module_repository = self.config[
                                    "cicd_https_urls"]["modules"]

                        for path in Path(temp_dir).rglob('*.tf'):
                            path_name = path.relative_to(temp_dir)
                            self.change_module_source(f"{temp_dir}/{path_name}",
                                                      module_repository,
                                                      tag=module_tag)

                    else:
                        # Modules stage
                        self.copy_with_patterns(os.path.abspath("../"),
                                                temp_dir, self.modules_include)
                        stage = "modules"

                    if stage == "modules" and self.config["use_upstream"]:
                        continue

                    print(
                        f"Initializing Git repository in {temp_dir} for stage {stage}"
                    )
                    self.run_git(["init", "--initial-branch", "main"], temp_dir)

                    git_origin = None
                    if self.config["git"] == "ssh":
                        git_origin = self.config["cicd_ssh_urls"][repository]
                    elif self.config["git"] == "https":
                        git_origin = self.config["cicd_https_urls"][repository]

                    if git_origin:
                        print(f"Adding origin {git_origin} for stage {stage}")
                        self.run_git(["remote", "add", "origin", git_origin],
                                     temp_dir)

                        print(f"Fetching origin")
                        fetch_ok, fetch_stderr = self.run_git(
                            ["fetch", "origin", "main"],
                            temp_dir,
                            allow_fail=True)
                        print(f"Adding all files in Git for stage {stage}")
                        self.run_git(["add", "--all"], temp_dir)

                        print(f"Committing all files {stage}")
                        self.run_git(
                            ["commit", "-a", "-m", "Fabric FAST import"],
                            temp_dir)

                        if fetch_ok is not None:
                            print(f"Rebasing {stage}")
                            self.run_git(["rebase", "origin/main", "main"],
                                         temp_dir)

                        print(f"Pushing commit for {stage}")
                        self.run_git(["push", "origin", "main"], temp_dir)
            return True
        return False


class FastCicdSystemConfigurator(FastDialogs):

    def __init__(self, session, config):
        super().__init__(session)
        self.config = {**self.config, **super().config}
        self.config = {**self.config, **config}

    def select_final_config(self):
        cicd_output_path = "stages/00-cicd/terraform.tfvars"
        choice = self.input_dialog(
            "final_config",
            f"CI/CD Terraform configuration",
            "Write the CI/CD stage Terraform configuration to the following file:",
            cicd_output_path,
        )
        if choice:
            self.config["cicd_output_path"] = cicd_output_path
            self.write_cicd_config(cicd_output_path)
            self.write_bootstrap_config(self.config["bootstrap_output_path"])
            return True
        return False


class FastGitlabConfigurator(FastCicdSystemConfigurator):
    config = {
        "gitlab_url": None,
        "gitlab_group": None,
        "gitlab_group_create": False,
    }
    setup_wizard = [
        "upstream_modules", "gitlab_url", "gitlab_token", "gitlab_group",
        "gitlab_repositories", "final_config"
    ]

    def __init__(self, session, config):
        super().__init__(session, config)
        self.config = {**self.config, **config}
        self.config["gitlab_repositories"] = {}
        for k, v in self.repositories.items():
            self.config["gitlab_repositories"][k] = k

    def select_gitlab_url(self):
        if self.config["cicd"] == "gitlab-ce":
            default_url = f"https://gitlab.{self.config['domain']}"
            choice = self.input_dialog(
                "gitlab_url",
                f"Gitlab installation URL",
                "Enter your Gitlab CE installation URL:",
                default_url,
            )
            if choice:
                self.config["gitlab_url"] = choice
                return True
            return False
        else:
            self.config["gitlab_url"] = "https://gitlab.com"
            return True

    def select_gitlab_token(self):
        if not os.getenv("GITLAB_TOKEN"):
            choice = self.input_dialog(
                "gitlab_url",
                f"Gitlab access token",
                "You don't seem to have GITLAB_TOKEN environment variable set.\n\nPlease enter your token below:",
                "",
                password=True)
            if choice:
                os.environ["GITLAB_TOKEN"] = choice
            return True
        return True

    def get_gitlab(self):
        return gitlab.Gitlab(self.config["gitlab_url"],
                             private_token=os.environ["GITLAB_TOKEN"])

    def select_gitlab_group(self):
        gl = self.get_gitlab()
        groups = gl.groups.list()
        values = {}
        for group in groups:
            if group.name != "GitLab Instance":
                values[group.path] = group.name
        values["_"] = "New group (enter name below)"

        (got_choice, choice) = self.select_with_input_dialog(
            "gitlab_group", "Select Gitlab group for repositories",
            "Select existing Gitlab group for repositories, or specify a new one.",
            "New group name:", values, self.config["gitlab_group"])
        if got_choice:
            self.config["gitlab_group"] = choice
            self.config["gitlab_group_create"] = False
            if choice not in values:
                self.config["gitlab_group_create"] = True
            return True
        return False

    def select_gitlab_repositories(self):
        values = {}
        if self.config["use_upstream"]:
            self.config["gitlab_repositories"].pop("modules")

        for k, v in self.config["gitlab_repositories"].items():
            values[k] = f"{self.repositories[k]}:"
        (got_choice, choice) = self.multi_input_dialog(
            "gitlab_repositories", "Gitlab repositories",
            "Customize Gitlab repository names. You can leave any empty if you don't want a repository for that stage.",
            values, self.config["gitlab_repositories"])
        if got_choice:
            self.config["gitlab_repositories"] = choice
            return True
        return False


class FastGithubConfigurator(FastCicdSystemConfigurator):
    config = {
        "github_organization": None,
    }
    setup_wizard = [
        "upstream_modules", "github_token", "github_organization",
        "github_repositories", "final_config"
    ]

    def __init__(self, session, config):
        super().__init__(session, config)
        self.config = {**self.config, **config}
        self.config["github_repositories"] = {}
        for k, v in self.repositories.items():
            self.config["github_repositories"][k] = k

    def select_github_token(self):
        if not os.getenv("GITHUB_TOKEN"):
            choice = self.input_dialog(
                "github_token",
                f"GitHub access token",
                "You don't seem to have GITHUB_TOKEN environment variable set.\n\nPlease enter your token below:",
                "",
                password=True)
            if choice:
                os.environ["GITHUB_TOKEN"] = choice
            return True
        return True

    def get_github(self):
        return Github(os.environ["GITHUB_TOKEN"])

    def select_github_organization(self):
        gh = self.get_github()
        organizations = gh.get_user().get_orgs()
        values = {}
        for org in organizations:
            values[org.login] = org.name if org.name else org.login

        (got_choice, choice) = self.select_dialog(
            "github_group", "Select GitHub organization",
            "Select the GitHub organization for the repositories.", values,
            self.config["github_organization"])
        if got_choice:
            self.config["github_organization"] = choice
            return True
        return False

    def select_github_repositories(self):
        values = {}
        if self.config["use_upstream"]:
            self.config["github_repositories"].pop("modules")

        for k, v in self.config["github_repositories"].items():
            values[k] = f"{self.repositories[k]}:"
        (got_choice, choice) = self.multi_input_dialog(
            "github_repositories", "GitHub repositories",
            "Customize GitHub repository names. You can leave any empty if you don't want a repository for that stage.",
            values, self.config["github_repositories"])
        if got_choice:
            self.config["github_repositories"] = choice
            return True
        return False


class FastConfigurator(FastDialogs):
    config = {
        "billing_account": None,
        "billing_account_in_org": None,
        "billing_account_org": None,
        "organization": None,
        "networking_model": "nva",
        "cicd": "custom",
        "directory_customer_id": None,
        "domain": None,
        "prefix": None,
    }
    setup_wizard = [
        "billing_account", "organization", "billing_account_in_org",
        "billing_account_org", "prerequisites", "cicd", "networking", "prefix",
        "final_config"
    ]
    groups = [
        "gcp-billing-admins", "gcp-devops", "gcp-network-admins",
        "gcp-organization-admins", "gcp-security-admins", "gcp-support"
    ]

    def select_billing_account(self):
        service = discovery.build('cloudbilling',
                                  'v1',
                                  http=self.get_branded_http())
        ba_request = service.billingAccounts().list()
        ba_response = ba_request.execute()
        values = {}
        for ba in ba_response["billingAccounts"]:
            ba_id = ba["name"].split("/")[-1]
            values[ba["name"]] = f"{ba['displayName']} ({ba_id})"
        (got_choice,
         choice) = self.select_dialog("billing_account",
                                      "Select billing account to use",
                                      f"Select billing account to use", values,
                                      self.config['billing_account'])
        if got_choice:
            self.config['billing_account'] = choice
        return got_choice

    def pick_organization(self, id, title, text, value):
        service = discovery.build('cloudresourcemanager',
                                  'v3',
                                  http=self.get_branded_http())
        org_request = service.organizations().search()
        org_response = org_request.execute()
        values = {}
        for org in org_response["organizations"]:
            values[org["name"]] = "%s (%s)" % (org["displayName"],
                                               org["name"].split("/")[1])

        return self.select_dialog(id, title, text, values, value)

    def select_organization(self):
        (got_choice, choice) = self.pick_organization(
            "organization", "Select organization",
            "Select organization to deploy Fabric FAST into",
            self.config["organization"])
        if got_choice:
            service = discovery.build('cloudresourcemanager',
                                      'v3',
                                      http=self.get_branded_http())
            org_request = service.organizations().get(name=choice)
            org_response = org_request.execute()
            self.config["domain"] = org_response["displayName"]
            self.config["directory_customer_id"] = org_response[
                "directoryCustomerId"]
            self.config["organization"] = choice
        return got_choice

    def select_billing_account_in_org(self):
        (got_choice, choice) = self.yesno_dialog(
            "billing_account_in_org", f"Billing account in organization",
            f"Does the billing account {self.config['billing_account']} belong\nto the organization {self.config['organization']}?\n\n(this cannot be checked automatically yet)"
        )
        if got_choice:
            self.config["billing_account_in_org"] = choice
        return got_choice

    def select_billing_account_org(self):
        if not self.config["billing_account_in_org"]:
            ba_id = self.config["billing_account"].split("/")[-1]
            (got_choice, choice) = self.pick_organization(
                "billing_account_org", "Select billing account organization",
                f"Select which organization the billing account {ba_id} belongs to:",
                self.config["billing_account_org"])
            if got_choice:
                self.config["billing_account_org"] = choice
            return got_choice
        else:
            self.config["billing_account_org"] = self.config["organization"]
        return True

    def select_prerequisites(self):

        user_account = self.get_current_user_from_gcloud()
        if "gserviceaccount.com" in user_account:
            user_account_iam = f"serviceAccount:{user_account}"
        else:
            user_account_iam = f"user:{user_account}"

        service = discovery.build('cloudresourcemanager',
                                  'v3',
                                  http=self.get_branded_http())
        iam_request = service.organizations().getIamPolicy(
            resource=self.config["organization"],
            body={"options": {
                "requestedPolicyVersion": 3
            }})
        iam_response = iam_request.execute()
        has_bindings = {
            "roles/billing.admin": False,
            "roles/logging.admin": False,
            "roles/iam.organizationRoleAdmin": False,
            "roles/resourcemanager.projectCreator": False,
        }
        to_add = dict(has_bindings)
        for binding in iam_response["bindings"]:
            if binding["role"] in has_bindings:
                has_bindings[binding["role"]] = True
                if user_account_iam not in binding["members"]:
                    binding["members"].append(user_account_iam)
                    to_add[binding["role"]] = True

        for role, binding_exist in has_bindings.items():
            if not binding_exist:
                iam_response["bindings"].append({
                    "role": role,
                    "members": [user_account_iam],
                })
                to_add[role] = True

        if not self.config["billing_account_in_org"]:
            ba_iam_request = service.organizations().getIamPolicy(
                resource=self.config["billing_account_org"],
                body={"options": {
                    "requestedPolicyVersion": 3
                }})
            ba_iam_response = iam_request.execute()

        anything_to_add = False
        text = f"The following IAM roles in {self.config['organization']} will be added for: {user_account}\n"
        for role, will_be_added in to_add.items():
            if will_be_added:
                text += f"\n  - {role}"
                anything_to_add = True

        if anything_to_add:
            choice = self.confirm_dialog(
                "prerequisites",
                f"Adding bootstrap permissions",
                text,
            )
            if choice:
                set_iam_request = service.organizations().setIamPolicy(
                    resource=self.config["organization"],
                    body={"policy": iam_response})
                set_iam_response = set_iam_request.execute()
            else:
                return False

        return True

    def select_prefix(self):
        default_prefix = self.config["domain"].replace(".", "")[0:4]
        choice = self.input_dialog(
            "prefix",
            f"Resource prefix",
            "Select your own short organization resource prefix (eg. 4 letters):",
            default_prefix,
        )
        if choice:
            self.config["prefix"] = choice
            return True
        return False

    def select_final_config(self):
        bootstrap_output_path = "stages/00-bootstrap/terraform.tfvars"
        choice = self.input_dialog(
            "final_config",
            f"Bootstrap Terraform configuration",
            "Write the bootstrap stage Terraform configuration to the following file:",
            bootstrap_output_path,
        )
        if choice:
            self.config["bootstrap_output_path"] = bootstrap_output_path
            self.write_bootstrap_config(bootstrap_output_path)
            self.terraform_format(bootstrap_output_path)
            return True
        return False

    def select_cicd(self):
        (got_choice, choice) = self.select_dialog(
            "cicd", "Select CI/CD",
            "Select the CI/CD system that you will be using.", {
                "github": "GitHub",
                "gitlab-com": "Gitlab.com",
                "gitlab-ce": "Gitlab (self-hosted)",
                "cloudbuild": "Cloud Build",
                "cloudbuild": "Cloud Source Repositories and Cloud Build",
                "custom": "Roll your own (custom CI/CD)",
            }, self.config['cicd'])
        if got_choice:
            self.config['cicd'] = choice
        return got_choice

    def select_networking(self):
        (got_choice, choice) = self.select_dialog(
            "networking", "Select networking model",
            f"You can pick between multiple networking models for your organization.\nFor more information, see the FAST repository.",
            {
                "vpn":
                    "Connectivity between hub and spokes via VPN HA tunnels",
                "nva":
                    "Connectivity between hub and the spokes via VPC network peering (supports network virtual appliances)",
                "peering":
                    "Connectivity between hub and spokes via VPC peering",
            }, self.config['networking_model'])
        if got_choice:
            self.config['networking_model'] = choice
        return got_choice


def main():
    session = PromptSession(cursor=CursorShape.BLINKING_BLOCK)
    print(f"Fabric FAST configurator v{VERSION}")

    configurator = FastConfigurator(session)
    config = configurator.run_wizard()

    if "gitlab" in config["cicd"] or config["cicd"] == "github":
        if "gitlab" in config["cicd"]:
            gitlab_configurator = FastGitlabConfigurator(session, config)
            config = gitlab_configurator.run_wizard()
        elif config["cicd"] == "github":
            github_configurator = FastGithubConfigurator(session, config)
            config = github_configurator.run_wizard()

        cicd_configurator = FastCicdConfigurator(session, config)
        config = cicd_configurator.run_wizard()

    print("All done!")


if __name__ == "__main__":
    main()
