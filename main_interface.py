import sys
from qt_bootstrap import ensure_qt_platform_plugin_path


from PyQt5.QtWidgets import QApplication, QMainWindow, QStackedWidget

# Import the modularized components
from gui_menu import MainMenu
from exp_initializer import NewExperimentWizard, ExperimentPropertiesPage


class MainInterface(QMainWindow):
    """Main application window that manages different pages"""
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Touchpad Experiment Manager")
        self.resize(1200, 800)
        
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        
        self.main_menu = MainMenu(self)
        self.new_experiment = NewExperimentWizard(self)
        self.experiment_properties = ExperimentPropertiesPage(self)
        
        self.stack.addWidget(self.main_menu)
        self.stack.addWidget(self.new_experiment)
        self.stack.addWidget(self.experiment_properties)

        # Always start on the home screen.
        self.show_main_menu()
        
    def show_new_experiment(self):
        self.stack.setCurrentWidget(self.new_experiment)

    def show_main_menu(self):
        self.stack.setCurrentWidget(self.main_menu)
        
    def show_experiment_properties(self, audio_groups, loaded_properties=None):
        self.experiment_properties.set_data(audio_groups, loaded_properties)
        self.stack.setCurrentWidget(self.experiment_properties)


if __name__ == "__main__":
    ensure_qt_platform_plugin_path()
    app = QApplication(sys.argv)
    window = MainInterface()
    window.show()
    sys.exit(app.exec_())
