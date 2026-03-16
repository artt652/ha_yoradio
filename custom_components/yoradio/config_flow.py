"""Config flow for ёRadio integration."""
from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
import homeassistant.helpers.config_validation as cv

from .const import (
    CONF_MAX_VOLUME,
    CONF_ROOT_TOPIC,
    DEFAULT_MAX_VOLUME,
    DEFAULT_NAME,
    DEFAULT_ROOT_TOPIC,
    DOMAIN,
)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ROOT_TOPIC, default=DEFAULT_ROOT_TOPIC): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_MAX_VOLUME, default=DEFAULT_MAX_VOLUME): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=254)
        ),
    }
)


class YoRadioConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for ёRadio."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            root_topic = user_input[CONF_ROOT_TOPIC].strip("/")

            # Prevent duplicate entries for the same MQTT root topic
            await self.async_set_unique_id(root_topic)
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=user_input.get(CONF_NAME, DEFAULT_NAME),
                data=user_input,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow handler."""
        return YoRadioOptionsFlow(config_entry)


class YoRadioOptionsFlow(config_entries.OptionsFlow):
    """Handle ёRadio options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        errors = {}

        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.data

        options_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_NAME,
                    default=current.get(CONF_NAME, DEFAULT_NAME),
                ): cv.string,
                vol.Optional(
                    CONF_MAX_VOLUME,
                    default=current.get(CONF_MAX_VOLUME, DEFAULT_MAX_VOLUME),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=254)),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=options_schema,
            errors=errors,
        )
