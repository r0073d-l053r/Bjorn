"""idle.py - No-op placeholder action for when Bjorn has nothing to do."""

from shared import SharedData

b_class = "IDLE"   
b_module = "idle" 
b_status = "IDLE"  


class IDLE:
    def __init__(self, shared_data):
        self.shared_data = shared_data
        


    
