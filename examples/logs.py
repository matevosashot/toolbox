from toolbox import setup_loggers, report_errors
import logging

@report_errors(raise_error=True, )
def function_that_raises_error():
    raise Exception("This is an error message")

if __name__ == "__main__":
    print("Setting up loggers")
    logger = setup_loggers(base_path="./logs", debug=True, telegram=True, train_logger=True, stdout=True)
    
    train_logger = logging.getLogger("main.train")

    try:
        function_that_raises_error()
    except Exception:
        pass

    print("Logging messages")
    logger.debug("This is a debug message")
    logger.info("This is an info message")
    logger.warning("This is a warning message")
    logger.error("This is an error message")
    
    train_logger.debug("This is a debug message to the train logger")
    train_logger.info("This is an info message to the train logger")
    train_logger.warning("This is a warning message to the train logger")
    train_logger.error("This is an error message to the train logger")
    

    