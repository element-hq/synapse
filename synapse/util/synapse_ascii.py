SYNAPSE_LOGO = '''  
  ____                                   
 / ___| _   _ _ __   __ _ _ __  ___  ___ 
 \___ \| | | | '_ \ / _` | '_ \/ __|/ _ \\
  ___) | |_| | | | | (_| | |_) \__ \  __/
 |____/ \__, |_| |_|\__,_| .__/|___/\___|
        |___/            |_|             
'''

def get_logo(version: str) -> str:
    """Returns the Synapse ASCII art logo."""
    return SYNAPSE_LOGO + "\t\t\t\t\t\t" + version + "\n"
