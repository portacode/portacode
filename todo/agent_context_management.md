# Agent Context Management

This document describes how the unibot portacode agent is expected to access contextual information to complete show awerness of user data. It is in the form of phases to be implemented one by one.


# Phase one: Project selection

- Each account object should carry some state data, which needs to include the fields "Selected Project": project UUID (can be null and default is null)
- A user can only select a project if they are authorized to access that project. And by default, no pojects are selected.
- project selection data should be accessible through the Account model
- the agent/bot needs to be equipped with a tool to select/unselect projects
- The system message of the agent is affected by which project is currently selected. If non, in contains data similar to that in the dashboard page. If a project is selected, it shows data similar to that of the project page. 
