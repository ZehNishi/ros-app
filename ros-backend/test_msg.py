import sys
sys.path.append("/home/jose/ros-app/ros-backend")
from app.ros.ros_client import ros_client
ros_client.init()
print(ros_client.get_message_fields("sensor_msgs/Imu"))
