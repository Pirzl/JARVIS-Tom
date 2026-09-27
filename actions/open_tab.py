from actions.browser_control import browser_control

TOOL = {
    "name": "open_tab",
    "description": "Opens a new tab with the specified URL in the browser.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "url": {
                "type": "STRING",
                "description": "The URL to open in a new tab. Must be a valid URL starting with http/https."
            },
            "browser": {
                "type": "STRING",
                "description": "Target browser (optional). Omit to use the currently active browser."
            }
        },
        "required": ["url"]
    },
    "handler": lambda parameters, player=None: browser_control({**parameters, "action": "new_tab"}, player)
}
