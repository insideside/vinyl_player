@echo off
set APP_HOME=%~dp0
set CLASSPATH=%APP_HOME%gradle\wrapper\gradle-wrapper.jar
if not exist "%CLASSPATH%" (
    echo Downloading Gradle wrapper...
    mkdir "%APP_HOME%gradle\wrapper" 2>nul
    curl -fsSL -o "%CLASSPATH%" "https://raw.githubusercontent.com/gradle/gradle/v8.5.0/gradle/wrapper/gradle-wrapper.jar"
)
java -classpath "%CLASSPATH%" org.gradle.wrapper.GradleWrapperMain %*
