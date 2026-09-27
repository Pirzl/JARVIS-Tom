from actions.browser_control import browser_control

TOOL = {
    "name": "switch_tab",
    "description": "Switches to an existing tab in the browser.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "tab_index": {
                "type": "INTEGER",
                "description": "The index of the tab to switch to (e.g. 0, 1, 2)."
            },
            "browser": {
                "type": "STRING",
                "description": "Target browser (optional). Omit to use the currently active browser."
            }
        },
        "required": ["tab_index"]
    },
    "handler": lambda parameters, player=None: browser_control({**parameters, "action": "switch"}, player)
}
