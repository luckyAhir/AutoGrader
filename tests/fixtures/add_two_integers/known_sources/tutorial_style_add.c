/* Known source: the typical "beginner tutorial" version of this program,
   kept so that verbatim copies of tutorial code are recognised. */
#include <stdio.h>

int main() {
    int number1, number2, sum;

    printf("Enter two integers: ");
    scanf("%d %d", &number1, &number2);

    // calculating sum
    sum = number1 + number2;

    printf("%d + %d = %d", number1, number2, sum);
    return 0;
}
