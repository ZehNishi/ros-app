import os
import socket
from urllib.parse import urlparse
import rospy

def check():
    uri = "http://localhost:11311"
    parsed = urlparse(uri)
    print("Testing reachable:", uri)
    try:
        conn = socket.create_connection((parsed.hostname, parsed.port), timeout=3)
        conn.close()
        print("Reachable: YES")
    except Exception as e:
        print("Reachable: NO", e)

    os.environ["ROS_MASTER_URI"] = uri
    os.environ["ROS_IP"] = "127.0.0.1"
    print("Init node...")
    try:
        rospy.init_node("test_node", anonymous=True, disable_signals=True)
        print("Init node: SUCCESS")
    except Exception as e:
        print("Init node: FAILED", e)

check()
