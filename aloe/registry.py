# -*- coding: utf-8 -*-
"""
Registry of steps and hooks called when running scenarios.
"""

import re
from collections import OrderedDict
from functools import wraps, partial

from aloe.codegen import multi_manager
from aloe.exceptions import undefined_step, StepLoadingError
from aloe.utils import unwrap_function


# What part of the test to hook
HOOK_WHAT = ("all", "feature", "example", "step")


# When to execute the hook in relation to the test part
HOOK_WHEN = ("before", "after", "around")


class PriorityClass(object):
    """
    Priority class constants.
    """

    DISPLAY = -20  # Display callbacks run first/last
    SYSTEM_OUTER = -10  # System callbacks executing before all except DISPLAY
    USER = 0  # User callbacks
    SYSTEM_INNER = 10  # System callbacks executing after others

    # Note that for 'after' and the 'after' part of 'around' callbacks,
    # the order is reversed


class CallbackDict(dict):
    """
    A collection of callbacks for all situations.
    """

    def __init__(self):
        """
        Initialize the callback lists for every kind of situation.
        """
        super().__init__({what: {when: {} for when in HOOK_WHEN} for what in HOOK_WHAT})

    @classmethod
    def _function_id(cls, func):
        """
        A unique identifier of a function to prevent adding the same hook
        twice.

        To support dynamically generated functions, take the variables from
        the function closure into account.
        """

        func = unwrap_function(func)

        return (
            func.__code__.co_filename,
            func.__code__.co_firstlineno,
            # variables in the closure might not be hashable
            tuple(str(c.cell_contents) for c in func.__closure__ or ()),
        )

    # pylint:disable=too-many-arguments
    def append_to(self, what, when, function, name=None, priority=0):
        """
        Add a callback for a particular type of hook.
        """
        if name is None:
            name = self._function_id(function)

        funcs = self[what][when].setdefault(priority, OrderedDict())
        funcs.pop(name, None)
        funcs[name] = function

    # pylint:enable=too-many-arguments

    def clear(self, name=None, priority_class=None):
        """
        Remove matching callbacks.
        If name is given, only remove callbacks with given name.
        If a priority class is given, only remove ones with the given class.
        """
        for what_dict in self.values():
            for when_dict in what_dict.values():
                if priority_class is None:
                    action_values = when_dict.values()
                else:
                    action_values = (value for (pc, _), value in when_dict.items() if pc == priority_class)
                for callback_list in action_values:
                    if name is None:
                        callback_list.clear()
                    else:
                        callback_list.pop(name, None)

    def hook_list(self, what, when):
        """
        Get all the hooks for a certain event, sorted appropriately.
        """
        return tuple(
            func for priority in sorted(self[what][when].keys()) for func in self[what][when][priority].values()
        )

    def wrap(self, what, function, *hook_args, **hook_kwargs):
        """
        Return a function that executes all the callbacks in proper relations
        to the given test part.
        """

        before_hooks = self.hook_list(what, "before")
        around_hooks = self.hook_list(what, "around")
        after_hooks = self.hook_list(what, "after")

        multi_hook = multi_manager(*around_hooks)

        @wraps(function)
        def wrapped(*args, **kwargs):
            """Run all the hooks in proper relations to the event."""
            for before_hook in before_hooks:
                before_hook(*hook_args, **hook_kwargs)

            try:
                with multi_hook(*hook_args, **hook_kwargs):
                    return function(*args, **kwargs)
            finally:
                # 'after' hooks still run after an exception
                for after_hook in reversed(after_hooks):
                    after_hook(*hook_args, **hook_kwargs)

        return wrapped

    def before_after(self, what):
        """
        Return a pair of functions to execute before and after the event.
        """

        before_hooks = self.hook_list(what, "before")
        around_hooks = self.hook_list(what, "around")
        after_hooks = self.hook_list(what, "after")

        multi_hook = multi_manager(*around_hooks)

        # Save in a closure for both functions
        around_hook = [None]

        def before_func(*args, **kwargs):
            """All hooks to be called before the event."""
            for before_hook in before_hooks:
                before_hook(*args, **kwargs)

            around_hook[0] = multi_hook(*args, **kwargs)
            around_hook[0].__enter__()

        def after_func(*args, **kwargs):
            """All hooks to be called after the event."""
            around_hook[0].__exit__(None, None, None)
            around_hook[0] = None

            for after_hook in after_hooks:
                after_hook(*args, **kwargs)

        return before_func, after_func


class StepDict(object):
    """
    A mapping of step sentences to their definitions.
    """

    def __init__(self):
        self.steps = dict()

    def load(self, sentence, func):
        """Add a mapping between a step sentence and a function."""

        step_re = self._assert_is_step(sentence, func)
        self.steps[step_re.pattern] = (step_re, func)

        try:
            func.sentence = sentence
            func.unregister = partial(self.unload_func, func)
        except AttributeError:
            # func might have been a bound method, no way to set attributes
            # on that
            pass

        return func

    def unload(self, sentence):
        """Remove a mapping for a given step sentence, if it exists."""
        try:
            del self.steps[sentence]
        except KeyError:
            pass

    def unload_func(self, func):
        """Remove any mappings for a given function."""

        sentences_to_remove = list(sentence for sentence, (_, step_func) in self.steps.items() if step_func == func)
        for sentence in sentences_to_remove:
            del self.steps[sentence]

    def clear(self):
        """Remove all registered steps."""
        self.steps.clear()

    def __len__(self):
        """Number of registered step sentences."""
        return len(self.steps)

    def load_func(self, func):
        """Load a step from a function."""
        sentence = self.extract_sentence(func)
        return self.load(sentence, func)

    def load_steps(self, obj):
        """Load steps from an object."""
        exclude = getattr(obj, "exclude", [])
        for attr in dir(obj):
            if self._attr_is_step(attr, obj) and attr not in exclude:
                step_method = getattr(obj, attr)
                self.load_func(step_method)
        return obj

    def extract_sentence(self, func):
        """Extract the step sentence from a function."""
        func = getattr(func, "__func__", func)
        sentence = getattr(func, "__doc__", None)
        if sentence is None:
            sentence = func.__name__.replace("_", " ")
            sentence = sentence[0].upper() + sentence[1:]
        return sentence

    def _assert_is_step(self, sentence, func):
        """Compile a step definition or raise an error."""
        try:
            if not sentence.endswith("$"):
                sentence += "$"
            return re.compile(sentence, re.I | re.U)
        except re.error as exc:
            raise StepLoadingError(
                "Error when trying to compile:\n"
                "  regex: %r\n"
                "  for function: %s\n"
                "  error: %s" % (sentence, func, exc)
            )

    def _attr_is_step(self, attr, obj):
        """Test whether an object's attribute is a step."""
        return attr[0] != "_" and self._is_func_or_method(getattr(obj, attr))

    def _is_func_or_method(self, func):
        """Test whether an object is a function or a method."""
        func_dir = dir(func)
        return callable(func) and ("func_name" in func_dir or "__func__" in func_dir)

    def match_step(self, step_):
        """
        Find a function and arguments to call for a specified Step.

        Returns a tuple of (function, args, kwargs).
        """
        # strip the first word which will be Given, Then, When or And
        # sentence = step_.sentence.split(' ', 1)[1]
        matched = None
        matched_func = None
        matched_pos = len(step_.sentence)

        for regex, func in self.steps.values():
            new_match = regex.search(step_.sentence)
            if new_match:
                pos = new_match.start(0)
                if pos < matched_pos:
                    matched = new_match
                    matched_func = func

        if matched:
            kwargs = matched.groupdict()
            if kwargs:
                return (matched_func, (), matched.groupdict())
            else:
                args = matched.groups()
                return (matched_func, args, {})

        return (undefined_step, (), {})

    def step(self, step_func_or_sentence):
        """
        Decorates a function, so that it will become a new step
        definition.

        You give step sentence either (by priority):

         * with step function argument;
         * with function doc; or
         * with the function name exploded by underscores.

        Parameters can be passed to steps using regular expressions.
        Parameters are passed in the order they are captured. Be aware that
        captured values are strings.

        The first parameter passed into the decorated function is the
        :class:`Step` object built for this step.

        Examples:

        .. code-block:: python

            @step("I go to the shops")
            def _i_go_to_the_shops_step(self):
                '''Implements I go to the shops'''

                ...

            @step
            def _i_go_to_the_shops_step(self):
                '''I go to the shops'''

                ...

            @step(r"I buy (\\d+) oranges")
            def _purchase_oranges_step(self, num_oranges):
                '''Buy a certain number of oranges'''

                num_oranges = int(num_oranges)

                ...

        Steps can be passed a table of data.

        .. code-block:: gherkin

            Given the following users are registered:
                | Username | Real name |
                | danni    | Danni     |
                | alexey   | Alexey    |

        This is exposed in the step as :attr:`Step.table` and
        :attr:`Step.hashes`.

        .. code-block:: python

            @step(r'Given the following users? (?:is|are) registered:')
            def _register_users(self):
                '''Register the given users'''

                for user in guess_types(self.hashes):
                    register(username=user['Username'],
                             realname=user['Real name'])

        Steps can be passed a multi-line "`Python string`".

        .. code-block:: gherkin

            Then I see a warning dialog:
                \"\"\"
                Changes could not be saved.

                [Try Again]
                \"\"\"

        This is exposed in the step as :attr:`Step.multiline`.

        The registered function will have an :code:`unregister()` method that
        removes all the step definitions that are associated with it.
        """

        if isinstance(step_func_or_sentence, bytes):
            # Python 2 strings, convert to str
            step_func_or_sentence = step_func_or_sentence.decode()

        if isinstance(step_func_or_sentence, str):
            return lambda func: self.load(step_func_or_sentence, func)
        else:
            return self.load_func(step_func_or_sentence)


STEP_REGISTRY = StepDict()


# This is a function, not a constant
# pylint:disable=invalid-name
step = STEP_REGISTRY.step
# pylint:enable=invalid-name


CALLBACK_REGISTRY = CallbackDict()


class CallbackDecorator(object):
    """
    Add functions to the appropriate callback lists.
    """

    def __init__(self, registry, when, priority_class=PriorityClass.USER):
        self.registry = registry
        self.when = when
        self.priority_class = priority_class

    def _decorate(self, what, function, name=None, priority=0):
        """
        Add the specified function (with name if given) to the callback list.
        """

        priority = (self.priority_class, priority)

        self.registry.append_to(what, self.when, function, name=name, priority=priority)
        return function

    def make_decorator(what):  # pylint:disable=no-self-argument
        """
        Make a decorator for a specific situation.

        NOTE: This is not a method of this class, just used to generate
        methods.
        """

        def decorator(self, function, **kwargs):
            """Decorator method for a particular situation."""
            # pylint:disable=protected-access
            return self._decorate(what, function, **kwargs)

        return decorator

    each_step = make_decorator("step")
    each_example = make_decorator("example")
    each_feature = make_decorator("feature")
    all = make_decorator("all")


# These are functions, not constants
# pylint:disable=invalid-name
after = CallbackDecorator(CALLBACK_REGISTRY, "after")
around = CallbackDecorator(CALLBACK_REGISTRY, "around")
before = CallbackDecorator(CALLBACK_REGISTRY, "before")
# pylint:enable=invalid-name
