# OpenCV Zoo 预处理与后处理

来源：https://github.com/opencv/opencv_zoo/tree/48b10fa505aeb51fbd127fc60a5c660040e823b7/models

四个 Python 文件基于上游代码，修改推理入口为 CPU 会话并修正 NMS 的 xywh 输入，分别遵循旁边的 Apache 2.0 LICENSE。Friday 的 ONNX Runtime 适配在包外实现；不随源码分发模型权重。
