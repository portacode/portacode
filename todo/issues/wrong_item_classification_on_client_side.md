Dockerfile in portacode_for_school is showing as a file in file explorer, but as an unknown file type. And the bigger problem is that in git status, while it shows in project stat as

      "staged_changes": [
        {
          "file_repo_path": "temporary_workspace/Dockerfile",
          "file_name": "Dockerfile",
          "file_abs_path": "/home/menas/portacode/portacode_for_school/temporary_workspace/Dockerfile",
          "change_type": "modified",
          "content_hash": "1d595ffac5d00445c25af13f46d86eb41d09c286ec4554487b9c169f6fec2fa9",
          "is_staged": true,
          "diff_details": null
        }
      ],

      which doesn't specify whether its a file or folder but the UI mistakes it for a folder and uses a folder icon